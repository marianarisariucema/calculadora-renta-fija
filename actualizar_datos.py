"""
Actualización diaria de datos para la Calculadora de renta fija.

Consulta, después del cierre del mercado:
  - BYMA, API Market Data End of Day: precios de renta fija (LECAP, LECER,
    Boncer, dólar linked) y futuros de dólar.
  - BCRA, API de Estadísticas Monetarias v4.0: CER (variable 30) y
    tipo de cambio mayorista de referencia Com. A 3500 (variable 5).

Genera el archivo datos.json que la calculadora lee al abrirse.

Credenciales (nunca se escriben en este archivo): se leen de las variables
de entorno BYMA_CLIENT_ID, BYMA_CLIENT_SECRET y BYMA_SCOPE.
Solo usa la biblioteca estándar de Python: no hace falta instalar nada.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

BYMA_TOKEN_URL = os.environ.get("BYMA_TOKEN_URL", "https://apigw.byma.com.ar/oauth/token/")
BYMA_EOD_URL = os.environ.get("BYMA_EOD_URL", "https://apigw.byma.com.ar/eod/v1")
BCRA_URL = os.environ.get("BCRA_URL", "https://api.bcra.gob.ar/estadisticas/v4.0/Monetarias")
SALIDA = os.environ.get("SALIDA", "datos.json")

# Grupos de renta fija donde están las letras y los bonos de la calculadora
GRUPOS_RF = ["LETRASTESORO", "LETRAS", "TITULOSPUBLICOS"]
# Plazos: 0001 = contado inmediato (T+0), 0002 = 24 horas (T+1)
PLAZOS = {"0001": "T0", "0002": "T1"}
MAX_PAGINAS = 50


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def pedir(url, data=None, headers=None, intentos=3):
    """GET (o POST si hay data) con reintentos; devuelve el JSON."""
    headers = dict(headers or {})
    headers.setdefault("Accept", "application/json")
    # El User-Agent por defecto de Python ("Python-urllib/3.x") suele ser rechazado por firewalls
    headers.setdefault("User-Agent", "Mozilla/5.0 (compatible; calculadora-renta-fija)")
    ultimo_error = None
    for i in range(intentos):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            ultimo_error = f"HTTP {e.code}"
            if e.code in (400, 401, 403, 404):
                break  # no tiene sentido reintentar
        except Exception as e:  # red, tiempo de espera, etc.
            ultimo_error = str(e)
        time.sleep(3 * (i + 1))
    raise RuntimeError(f"No se pudo consultar {url.split('?')[0]}: {ultimo_error}")


# ---------------------------------------------------------------- BYMA
def token_byma():
    cid, secret = os.environ.get("BYMA_CLIENT_ID"), os.environ.get("BYMA_CLIENT_SECRET")
    if not cid or not secret:
        raise RuntimeError("Faltan las credenciales BYMA_CLIENT_ID / BYMA_CLIENT_SECRET.")
    campos = {"client_id": cid, "client_secret": secret, "grant_type": "client_credentials"}
    if os.environ.get("BYMA_SCOPE"):
        campos["scope"] = os.environ["BYMA_SCOPE"]
    j = pedir(BYMA_TOKEN_URL, data=urllib.parse.urlencode(campos).encode(),
              headers={"Content-Type": "application/x-www-form-urlencoded"})
    tok = j.get("access_token")
    if not tok:
        raise RuntimeError("BYMA respondió sin access_token.")
    return tok


def eod(token, servicio, params):
    """Recorre todas las páginas de un servicio EOD y devuelve la lista de resultados."""
    filas, vistas = [], set()
    for pagina in range(MAX_PAGINAS):
        q = urllib.parse.urlencode({**params, "page": pagina})
        j = pedir(f"{BYMA_EOD_URL}/{servicio}.raw/?{q}", headers={"Authorization": f"Bearer {token}"})
        res = j.get("result") or []
        clave = lambda x: (x.get("security_id"), x.get("symbol"), x.get("settlPeriod"), x.get("settlDate"))
        nuevas = [x for x in res if clave(x) not in vistas]
        if not nuevas:
            break
        for x in nuevas:
            vistas.add(clave(x))
        filas.extend(nuevas)
    return filas


def precio_de_referencia(x):
    """Cierre; si no hay, último operado; si no operó, cierre anterior."""
    for campo, origen in (("closing_price", "cierre"), ("trade", "último"), ("previous_close", "cierre anterior")):
        v = x.get(campo)
        if isinstance(v, (int, float)) and v > 0:
            return float(v), origen
    return None, None


def precios_byma(token):
    precios = {v: {} for v in PLAZOS.values()}
    fecha = None
    for grupo in GRUPOS_RF:
        try:
            filas = eod(token, "fixed_income", {"group": grupo, "market": "PPT", "operativeform": "CONTADO",
                                                 "currency": "ARS", "settlperiod": ""})
        except RuntimeError as e:
            log(f"  {grupo}: {e}")
            continue
        n = 0
        for x in filas:
            plazo = PLAZOS.get(str(x.get("settlPeriod")))
            sym = str(x.get("symbol") or "").upper()
            px, origen = precio_de_referencia(x)
            if not plazo or not sym or px is None:
                continue
            precios[plazo][sym] = {"px": px, "origen": origen, "operaciones": x.get("trades", 0)}
            fecha = fecha or x.get("Date")
            n += 1
        log(f"  {grupo}: {n} precios")
    return precios, fecha


def futuros_byma(token):
    out = []
    try:
        filas = eod(token, "futures", {"group": "FUTMONEDAS"})
    except RuntimeError as e:
        log(f"  Futuros: {e}")
        return out
    for x in filas:
        px, origen = precio_de_referencia(x)
        vto = str(x.get("settlDate") or "")
        if px is None or len(vto) != 8:
            continue
        out.append({"symbol": x.get("symbol"), "vto": f"{vto[:4]}-{vto[4:6]}-{vto[6:]}", "px": px,
                    "origen": origen, "interes_abierto": x.get("open_interest")})
    log(f"  Futuros de moneda: {len(out)}")
    return sorted(out, key=lambda f: f["vto"])


# ---------------------------------------------------------------- BCRA
def serie_bcra(variable, desde):
    out, offset = {}, 0
    for _ in range(20):
        j = pedir(f"{BCRA_URL}/{variable}?Desde={desde}&Limit=3000&Offset={offset}")
        det = ((j.get("results") or [{}])[0] or {}).get("detalle") or []
        for d in det:
            if d.get("fecha") and (d.get("valor") or 0) > 0:
                out[str(d["fecha"])[:10]] = float(d["valor"])
        total = ((j.get("metadata") or {}).get("resultset") or {}).get("count") or 0
        offset += len(det)
        if not det or offset >= total:
            break
    return dict(sorted(out.items()))


# ---------------------------------------------------------------- principal
def main():
    datos = {"generado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "fuentes": {"precios": "BYMA - API Market Data End of Day",
                         "cer_a3500": "BCRA - API Estadísticas Monetarias v4.0 (variables 30 y 5)"}}
    errores = []   # fallas que la página no puede suplir (BYMA): la tarea termina con error
    avisos = []    # fallas que la página resuelve sola (BCRA): solo quedan como aviso

    log("BCRA: CER y A3500")
    try:
        desde = (date.today() - timedelta(days=60)).isoformat()
        datos["cer"] = serie_bcra(30, desde)
        fx = serie_bcra(5, desde)
        if fx:
            u = max(fx)
            datos["a3500"] = {"fecha": u, "valor": fx[u]}
        log(f"  CER: {len(datos['cer'])} datos; A3500: {datos.get('a3500')}")
    except RuntimeError as e:
        # La calculadora igual consulta al BCRA desde el navegador y trae el CER cargado,
        # así que esto no corta la tarea. Se avisa en GitHub como "warning".
        avisos.append(str(e)); log(f"  {e}")
        print(f"::warning::BCRA: {e}", flush=True)

    log("BYMA: precios de fin de día")
    if not os.environ.get("BYMA_CLIENT_ID") or not os.environ.get("BYMA_CLIENT_SECRET"):
        log("  Todavía no hay credenciales de BYMA cargadas: se omiten los precios (no es un error).")
    else:
      try:
        tok = token_byma()
        datos["precios"], datos["fecha_precios"] = precios_byma(tok)
        datos["futuros"] = futuros_byma(tok)
      except RuntimeError as e:
        errores.append(str(e)); log(f"  {e}")

    # Lo que no se pudo obtener hoy se conserva del archivo anterior
    if os.path.exists(SALIDA):
        try:
            prev = json.load(open(SALIDA, encoding="utf-8"))
            if "precios" not in datos:
                for k in ("precios", "fecha_precios", "futuros"):
                    if k in prev:
                        datos[k] = prev[k]
                log("  Se conservan los precios anteriores.")
            if "cer" not in datos:
                for k in ("cer", "a3500"):
                    if k in prev:
                        datos[k] = prev[k]
                log("  Se conservan el CER y el A3500 anteriores.")
        except Exception:
            pass

    datos["errores"] = errores
    if avisos:
        datos["avisos"] = avisos
    with open(SALIDA, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, indent=1)
    log(f"Listo: {SALIDA}")
    # Si algo falló, la tarea termina con error para que GitHub avise por mail
    # (el archivo igual se guarda con lo que sí se pudo obtener).
    if errores:
        sys.exit(1)


if __name__ == "__main__":
    main()
