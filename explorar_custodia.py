"""
Prueba de la API Custody Instruments de BYMA (custodia de instrumentos).

Descarga el listado de LETRAS y BONOS que están en custodia y muestra en el
registro de la tarea algunas columnas (código, nombre, ISIN, emisión, vencimiento)
para ver qué trae antes de conectarla a la calculadora. No modifica datos.json.

La API es asincrónica: el primer pedido responde 409 con un identificador (uuid);
se repite el pedido con la cabecera X-UUID hasta que el archivo está listo.

Credenciales: variables de entorno BYMA_CUSTODY_CLIENT_ID y BYMA_CUSTODY_CLIENT_SECRET.
"""
import csv
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request

TOKEN_URL = "https://api.byma.com.ar/oauth/token/"
BASE = "https://api.byma.com.ar/custody-instruments/v1"
CLASES = ["LETRA", "BONO"]
COLUMNAS = ["cvsaIdentifier", "instrumentLongName", "isin", "currency",
            "issueDate", "maturityDate", "activationTypeName"]
UA = "Mozilla/5.0 (compatible; calculadora-renta-fija)"


def http(url, data=None, headers=None):
    """Devuelve (código HTTP, texto de la respuesta) sin cortar en los errores."""
    headers = {"User-Agent": UA, **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, r.read().decode("utf-8-sig")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def token():
    cid, secret = os.environ.get("BYMA_CUSTODY_CLIENT_ID"), os.environ.get("BYMA_CUSTODY_CLIENT_SECRET")
    if not cid or not secret:
        sys.exit("Faltan los secrets BYMA_CUSTODY_CLIENT_ID / BYMA_CUSTODY_CLIENT_SECRET.")
    campos = {"client_id": cid, "client_secret": secret, "grant_type": "client_credentials",
              "scope": "custodyinstruments.read"}
    cod, txt = http(TOKEN_URL, urllib.parse.urlencode(campos).encode(),
                    {"Content-Type": "application/x-www-form-urlencoded"})
    if cod != 200:
        sys.exit(f"No se pudo obtener el token (HTTP {cod}): {txt[:300]}")
    return json.loads(txt)["access_token"]


def pedir_asincronico(tok, url, intentos=30):
    """Primer pedido -> 409 con uuid; luego se repite con X-UUID hasta tener el CSV."""
    h = {"Authorization": f"Bearer {tok}"}
    for i in range(intentos):
        cod, txt = http(url, headers=h)
        if cod == 409:
            try:
                h["X-UUID"] = json.loads(txt)["uuid"]
            except Exception:
                pass
            time.sleep(10)
            continue
        if cod == 200:
            return txt
        raise RuntimeError(f"HTTP {cod}: {txt[:300]}")
    raise RuntimeError("BYMA no terminó de preparar el archivo a tiempo.")


def main():
    tok = token()
    print("Token de custodia: OK")
    for clase in CLASES:
        url = f"{BASE}/standard.csv/?" + urllib.parse.urlencode({"assetClassName": clase})
        try:
            txt = pedir_asincronico(tok, url)
        except RuntimeError as e:
            print(f"\n{clase}: {e}")
            continue
        filas = list(csv.DictReader(io.StringIO(txt), delimiter=";"))
        activas = [f for f in filas if (f.get("activationTypeName") or "").lower() == "active"]
        print(f"\n===== {clase}: {len(filas)} instrumentos ({len(activas)} activos) =====")
        print(" | ".join(COLUMNAS))
        # Los que vencen más tarde primero: son los vigentes
        for f in sorted(activas, key=lambda f: f.get("maturityDate") or "", reverse=True)[:40]:
            print(" | ".join((f.get(c) or "").strip() for c in COLUMNAS))


if __name__ == "__main__":
    main()
