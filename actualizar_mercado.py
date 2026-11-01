"""
Robot de mercado para el panel "Mesa Personal".

Corre en GitHub Actions después del cierre (no en tu compu) y genera:
  - mercado.json: último precio de cada papel + variaciones, rango de 52 semanas,
    medias móviles y RSI; dólar MEP y CCL; CER, A3500, tasas y reservas del BCRA.
  - historial/<TICKER>.json: cierres diarios de cada papel (para gráficos y técnico).
  - diagnostico.json: qué servicios de la API de BYMA respondieron y cuántos papeles trajo cada uno.

Fuentes:
  - BYMA, API Market Data End of Day (credenciales en los secretos del repositorio).
  - BCRA, Estadísticas Monetarias v4.0 y Estadísticas Cambiarias v1.0 (públicas).
  - Yahoo Finance (público) solo para completar historial de acciones y CEDEARs.

Solo precios de mercado públicos: este archivo NO tiene ni debe tener posiciones ni clientes,
porque el repositorio es público.
"""
import json
import math
import os
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

BYMA_TOKEN_URL = os.environ.get("BYMA_TOKEN_URL", "https://apigw.byma.com.ar/oauth/token/")
BYMA_EOD_URL = os.environ.get("BYMA_EOD_URL", "https://apigw.byma.com.ar/eod/v1")
BCRA_MON = "https://api.bcra.gob.ar/estadisticas/v4.0/Monetarias"
BCRA_CAMB = "https://api.bcra.gob.ar/estadisticascambiarias/v1.0"
UA = {"User-Agent": "Mozilla/5.0 (robot-mercado; +github actions)"}

# Universo amplio (incluye panel líder, CEDEARs más operados, soberanos y CER).
# Es una lista genérica a propósito: no refleja ninguna cartera.
ACCIONES = ["ALUA", "BBAR", "BMA", "BYMA", "CEPU", "COME", "CRES", "EDN", "GGAL", "IRSA", "LOMA", "METR",
            "PAMP", "SUPV", "TECO2", "TGNO4", "TGSU2", "TRAN", "TXAR", "VALO", "YPFD", "VIST", "MIRG", "CVH", "HARG"]
CEDEARS = ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "NFLX", "AMD", "INTC", "ORCL", "IBM", "CSCO",
           "JPM", "BAC", "C", "WFC", "GS", "V", "MA", "PYPL", "KO", "PEP", "MCD", "WMT", "PG", "JNJ", "PFE", "MRK",
           "ABBV", "XOM", "CVX", "BRKB", "DIS", "BABA", "MELI", "NU", "GLOB", "VALE", "PBR", "ITUB", "BBD",
           "SPY", "QQQ", "DIA", "IWM", "EEM", "EWZ", "XLE", "XLF", "GLD", "SLV", "IBIT", "ETHA", "ARKK", "UBER", "SHOP",
           "COIN", "MSTR", "PLTR", "AVGO", "TSM", "ASML", "BIDU", "JD", "X", "GOLD", "NEM", "LLY", "UNH", "HD", "COST"]
# Grupos de renta fija y de renta variable a probar en la API EOD de BYMA.
GRUPOS_RF = ["LETRASTESORO", "LETRAS", "TITULOSPUBLICOS", "OBLIGACIONESNEGOCIABLES", "ON", "OBLIGACIONES"]
SERVICIOS_RV = [("equity", {}), ("equities", {}), ("stocks", {}), ("cedears", {}), ("equity", {"group": "ACCIONES"}),
                ("equity", {"group": "CEDEARS"}), ("equity", {"group": "LIDERES"}), ("equity", {"group": "GENERAL"})]
# Variables del BCRA (Estadísticas Monetarias v4.0). Se buscan por nombre en el maestro para no depender de ids.
BCRA_BUSCAR = {
    "reservas": ["Reservas Internacionales del BCRA"],
    "badlar": ["BADLAR en pesos de bancos privados (en % n.a.)", "BADLAR"],
    "tamar": ["TAMAR en pesos de bancos privados (en % n.a.)", "TAMAR"],
    "base_monetaria": ["Base monetaria"],
    "inflacion_mensual": ["Inflación mensual"],
    "inflacion_esperada": ["Inflación esperada - REM"],
}

diag = {"generado": datetime.now(timezone.utc).isoformat(timespec="seconds"), "byma": {}, "bcra": {}, "yahoo": {}, "errores": []}


def log(m):
    print(m, flush=True)


def pedir(url, data=None, headers=None, intentos=3):
    h = dict(UA, **(headers or {}))
    err = None
    for i in range(intentos):
        try:
            req = urllib.request.Request(url, data=data, headers=h, method="POST" if data else "GET")
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err = f"HTTP {e.code}"
            if e.code in (400, 401, 403, 404):
                break
        except Exception as e:  # noqa
            err = str(e)
        time.sleep(2 * (i + 1))
    raise RuntimeError(err)


# ------------------------------------------------------------------ BYMA
def token_byma():
    cid, sec = os.environ.get("BYMA_CLIENT_ID"), os.environ.get("BYMA_CLIENT_SECRET")
    if not cid or not sec:
        raise RuntimeError("faltan BYMA_CLIENT_ID / BYMA_CLIENT_SECRET")
    campos = {"client_id": cid, "client_secret": sec, "grant_type": "client_credentials"}
    if os.environ.get("BYMA_SCOPE"):
        campos["scope"] = os.environ["BYMA_SCOPE"]
    j = pedir(BYMA_TOKEN_URL, data=urllib.parse.urlencode(campos).encode(),
              headers={"Content-Type": "application/x-www-form-urlencoded"})
    return j["access_token"]


def eod(tok, servicio, params):
    filas, pagina = [], 1
    while pagina <= 50:
        q = urllib.parse.urlencode({**params, "page": pagina})
        j = pedir(f"{BYMA_EOD_URL}/{servicio}.raw/?{q}", headers={"Authorization": f"Bearer {tok}"})
        datos = j.get("data") if isinstance(j, dict) else j
        if not datos:
            break
        filas.extend(datos)
        tot = (j.get("pagination") or {}).get("totalPages") if isinstance(j, dict) else None
        if not tot or pagina >= tot:
            break
        pagina += 1
    return filas


def ref(x):
    for campo, origen in (("closing_price", "cierre"), ("trade", "último"), ("previous_close", "cierre anterior")):
        v = x.get(campo)
        if isinstance(v, (int, float)) and v > 0:
            return float(v), origen
    return None, None


def precios_byma():
    out, usd, fecha = {}, {}, None
    try:
        tok = token_byma()
    except Exception as e:
        diag["errores"].append(f"BYMA token: {e}")
        return out, usd, fecha

    def cargar(servicio, params, etiqueta, destino, solo_t1=True):
        nonlocal fecha
        try:
            filas = eod(tok, servicio, params)
        except Exception as e:
            diag["byma"][etiqueta] = f"error: {e}"
            return
        n = 0
        for x in filas:
            if solo_t1 and str(x.get("settlPeriod")) not in ("0002", "2", "None"):
                continue
            sym = str(x.get("symbol") or "").upper()
            px, origen = ref(x)
            if sym and px:
                destino[sym] = {"px": px, "origen": origen, "operaciones": x.get("trades", 0)}
                fecha = fecha or x.get("Date")
                n += 1
        diag["byma"][etiqueta] = n

    for g in GRUPOS_RF:
        cargar("fixed_income", {"group": g, "market": "PPT", "operativeform": "CONTADO", "currency": "ARS", "settlperiod": ""}, f"rf {g} ARS", out)
        cargar("fixed_income", {"group": g, "market": "PPT", "operativeform": "CONTADO", "currency": "USD", "settlperiod": ""}, f"rf {g} MEP", usd.setdefault("MEP", {}))
        cargar("fixed_income", {"group": g, "market": "PPT", "operativeform": "CONTADO", "currency": "EXT", "settlperiod": ""}, f"rf {g} CCL", usd.setdefault("CCL", {}))
    for servicio, params in SERVICIOS_RV:
        cargar(servicio, dict(params, market="BYMA", operativeform="CONTADO", currency="ARS", settlperiod=""),
               f"rv {servicio} {params.get('group', '')}".strip(), out)
    return out, usd, fecha


# ------------------------------------------------------------------ BCRA
def bcra():
    res = {}
    try:
        maestro = pedir(BCRA_MON).get("results", [])
        for clave, nombres in BCRA_BUSCAR.items():
            v = next((m for n in nombres for m in maestro if n.lower() in str(m.get("descripcion", "")).lower()), None)
            if v:
                res[clave] = {"valor": v.get("ultValorInformado"), "fecha": v.get("ultFechaInformada"),
                              "descripcion": v.get("descripcion"), "id": v.get("idVariable")}
        for var, clave in ((30, "cer"), (5, "a3500")):
            v = next((m for m in maestro if m.get("idVariable") == var), None)
            if v:
                res[clave] = {"valor": v.get("ultValorInformado"), "fecha": v.get("ultFechaInformada")}
        diag["bcra"]["monetarias"] = len(res)
    except Exception as e:
        diag["errores"].append(f"BCRA monetarias: {e}")
    try:
        c = pedir(f"{BCRA_CAMB}/Cotizaciones").get("results", {})
        usd = next((d for d in c.get("detalle", []) if d.get("codigoMoneda") == "USD"), None)
        if usd:
            res["usd_oficial"] = {"valor": usd.get("tipoCotizacion"), "fecha": c.get("fecha")}
        diag["bcra"]["cambiarias"] = "ok"
    except Exception as e:
        diag["errores"].append(f"BCRA cambiarias: {e}")
    return res


# ------------------------------------------------------------------ Yahoo (historial)
def yahoo(sym):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?range=1y&interval=1d"
    j = pedir(url, intentos=2)
    r = j["chart"]["result"][0]
    ts, cl = r.get("timestamp") or [], r["indicators"]["quote"][0].get("close") or []
    return [(datetime.fromtimestamp(t, timezone.utc).date().isoformat(), round(c, 4)) for t, c in zip(ts, cl) if c]


# ------------------------------------------------------------------ indicadores
def indicadores(cierres):
    if not cierres:
        return {}
    px = cierres[-1]
    d = {}
    if len(cierres) >= 2:
        d["var1d"] = round((px / cierres[-2] - 1) * 100, 2)
    if len(cierres) >= 6:
        d["var5d"] = round((px / cierres[-6] - 1) * 100, 2)
    ult = cierres[-252:]
    d["min52"], d["max52"] = min(ult), max(ult)
    for n in (20, 50, 200):
        if len(cierres) >= n:
            d[f"sma{n}"] = round(sum(cierres[-n:]) / n, 4)
    if len(cierres) >= 15:
        g = p = 0.0
        for a, b in zip(cierres[-15:-1], cierres[-14:]):
            ch = b - a
            g += max(ch, 0)
            p += max(-ch, 0)
        d["rsi14"] = round(100 - 100 / (1 + (g / p if p else math.inf)), 1) if p else 100.0
    return d


def leer_hist(t):
    try:
        with open(f"historial/{t}.json", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"fechas": [], "cierres": []}


def guardar_hist(t, h):
    os.makedirs("historial", exist_ok=True)
    with open(f"historial/{t}.json", "w", encoding="utf-8") as f:
        json.dump(h, f, separators=(",", ":"))


def main():
    hoy = date.today().isoformat()
    precios, usd, fecha_byma = precios_byma()
    fecha = (str(fecha_byma)[:10] if fecha_byma else hoy)

    # Historial: completar acciones y CEDEARs con Yahoo si hay menos de 200 ruedas guardadas
    for t in ACCIONES + CEDEARS:
        h = leer_hist(t)
        if len(h["cierres"]) < 200:
            try:
                serie = yahoo(t + ".BA")
                if serie:
                    h = {"fechas": [s[0] for s in serie], "cierres": [s[1] for s in serie], "fuente": "Yahoo Finance (.BA)"}
                    guardar_hist(t, h)
                    diag["yahoo"][t] = len(serie)
                    if t not in precios:
                        precios[t] = {"px": serie[-1][1], "origen": "Yahoo, último cierre"}
            except Exception as e:
                diag["yahoo"][t] = f"error: {e}"
            time.sleep(0.4)

    # Sumar el cierre de hoy al historial de cada papel con precio de BYMA
    salida = {}
    for t, p in precios.items():
        h = leer_hist(t)
        if p.get("origen") != "Yahoo, último cierre":
            if h["fechas"] and h["fechas"][-1] == fecha:
                h["cierres"][-1] = p["px"]
            else:
                h["fechas"].append(fecha)
                h["cierres"].append(p["px"])
            h["fechas"], h["cierres"] = h["fechas"][-400:], h["cierres"][-400:]
            guardar_hist(t, h)
        salida[t] = dict(p, **indicadores(h["cierres"]))

    # Dólar MEP y CCL implícitos con AL30 / GD30
    mep = ccl = None
    for b in ("AL30", "GD30"):
        ars = (precios.get(b) or {}).get("px")
        if ars and not mep and (usd.get("MEP", {}).get(b + "D") or {}).get("px"):
            mep = round(ars / usd["MEP"][b + "D"]["px"], 2)
        if ars and not ccl and (usd.get("CCL", {}).get(b + "C") or {}).get("px"):
            ccl = round(ars / usd["CCL"][b + "C"]["px"], 2)

    datos = {"fecha": fecha, "generado": diag["generado"], "mep": mep, "ccl": ccl, "bcra": bcra(),
             "fuente": "BYMA Market Data End of Day (cierre T+1); historial de acciones y CEDEARs completado con Yahoo Finance",
             "precios": salida}
    if datos["bcra"].get("a3500"):
        datos["a3500"] = datos["bcra"]["a3500"]["valor"]
    with open("mercado.json", "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, separators=(",", ":"))
    with open("diagnostico.json", "w", encoding="utf-8") as f:
        json.dump(diag, f, ensure_ascii=False, indent=1)
    log(f"Listo: {len(salida)} papeles, MEP {mep}, CCL {ccl}")
    log(json.dumps(diag["byma"], ensure_ascii=False))


if __name__ == "__main__":
    main()
