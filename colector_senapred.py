#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GeoInformesChile — colector de alertas SENAPRED. (v3)

Novedad v3: fuente web REAL conectada. senapred.cl corre sobre AWS AppSync
(GraphQL) con credenciales temporales de visitante (Cognito). Este script:
  1. Pide un pase de visitante anónimo a Cognito (igual que hace el navegador).
  2. Firma la petición GraphQL con SigV4 (el sello que exige AWS).
  3. Consulta las alertas/eventos y las normaliza al esquema común.
Todo en Python estándar + requests: cero dependencias nuevas.

Fuentes: (1) AppSync GraphQL de senapred.cl  (2) capas ArcGIS  (3) Telegram.
Salidas: alerts.json (siempre) + historico.jsonl (append-only).
"""

import datetime as dt
import hashlib
import hmac
import json
import pathlib
import re
import sys
import unicodedata
from urllib.parse import urlparse

import requests

# ----------------------------- CONFIG ---------------------------------------

# AppSync de SENAPRED (capturado del propio sitio, 2026-07):
APPSYNC_URL = "https://rz2uv7ifxbgflh2bqmp6kmh4le.appsync-api.us-east-1.amazonaws.com/graphql"
AWS_REGION = "us-east-1"

# Pool de Cognito (repuesto duradero, opcional): en senapred.cl con F12 ->
# Application -> Local Storage -> https://www.senapred.cl -> el NOMBRE de la
# clave "CognitoIdentityId-<pool>": lo que sigue al guion es el pool.
IDENTITY_POOL_ID = ""

# Pase de visitante capturado (valor de esa misma clave). Funciona directo;
# si algún día caduca, se rellena el pool de arriba y se vacía este.
COGNITO_IDENTITY_ID = "us-east-1:951b2325-d793-c511-a892-02368a4848ac"

# Tipos de registro a pedir. "Alerta" es la apuesta principal; si la captura
# de senapred.cl/eventos muestra otro nombre de consulta/tipo, se ajusta aquí.
TIPOS_GRAPHQL = ["Alerta"]
LIMITE_GRAPHQL = 200          # registros por página
MAX_PAGINAS = 3               # tope de paginación por corrida

# TODO(B): capas ArcGIS del dashboard "ALERTAS SENAPRED" (respaldo).
ARCGIS_LAYERS = []

VIGENCIA_DIAS = 7             # alertas telegram/web sin renovar -> EXPIRADA
TIMEOUT = 25
DIR = pathlib.Path(__file__).parent
SALIDA = DIR / "alerts.json"
HISTORICO = DIR / "historico.jsonl"
TELEGRAM_JSONL = DIR / "telegram_alertas.jsonl"

UA = {"User-Agent": "GeoInformesChile/0.3 (colector de utilidad publica; fuente: SENAPRED)"}

# ----------------------------- HELPERS ---------------------------------------

def ahora_dt():
    return dt.datetime.now(dt.timezone.utc)


def ahora():
    return ahora_dt().isoformat(timespec="seconds")


def slug(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().split())


REGIONES_BCN = [
    ("arica", "Arica y Parinacota"), ("tarapac", "Tarapacá"),
    ("antofagasta", "Antofagasta"), ("atacama", "Atacama"),
    ("coquimbo", "Coquimbo"), ("valpara", "Valparaíso"),
    ("metropolitana", "Metropolitana de Santiago"),
    ("higgins", "Libertador General Bernardo O'Higgins"),
    ("maule", "Maule"), ("nuble", "Ñuble"), ("biobio", "Biobío"),
    ("araucan", "La Araucanía"), ("los rios", "Los Ríos"),
    ("los lagos", "Los Lagos"), ("aysen", "Aysén del General Carlos Ibáñez del Campo"),
    ("magallanes", "Magallanes y de la Antártica Chilena"),
]


def region_canon(nombre):
    """Cualquier forma ('Región del Maule', 'O’Higgins') -> nombre BCN del geojson."""
    t = slug(nombre)
    for clave, canon in REGIONES_BCN:
        if clave in t:
            return canon
    # Guardia anti-basura: si lo capturado es una migaja ("de", "la"...), fuera.
    if len(t) < 4 or t in ("de", "del", "de la", "los", "las", "el"):
        return ""
    return str(nombre).strip()


def nivel_norm(s):
    t = slug(s)
    if "roja" in t:
        return "ROJA"
    if "amarilla" in t:
        return "AMARILLA"
    if "temprana" in t or "preventiva" in t or t in ("atp", "verde"):
        return "ATP"
    return None


TIPOS = {
    "aluvi": "aluvión", "crecid": "crecida de río", "desbord": "desborde",
    "remoci": "remoción en masa", "quebrad": "activación de quebradas",
    "marejad": "marejadas", "tormenta": "tormentas eléctricas", "viento": "viento",
    "nevad": "nevadas", "lluvia": "evento meteorológico",
    "meteorol": "evento meteorológico", "incendio": "incendio forestal",
    "volc": "actividad volcánica", "tsunami": "tsunami", "evacu": "evacuación",
}


def tipo_de(texto):
    t = slug(texto)
    for k, v in TIPOS.items():
        if k in t:
            return v
    return "otro"


def make_id(*parts):
    base = "|".join(slug(p) for p in parts if p)
    return hashlib.sha1(base.encode()).hexdigest()[:12]


def pick(d, *candidatos):
    idx = {slug(k): k for k in d.keys()}
    for c in candidatos:
        k = idx.get(slug(c))
        if k is not None and d[k] not in (None, ""):
            return d[k]
    return None


def separar_comunas(raw):
    if isinstance(raw, list):
        piezas = [str(c) for c in raw]
    else:
        piezas = re.split(r",|;|\s+y\s+", str(raw))
    return [c.strip() for c in piezas if c.strip()]


def antiguedad_dias(ts):
    try:
        t = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        return (ahora_dt() - t).total_seconds() / 86400
    except Exception:
        return None


def sin_html(texto):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(texto or ""))).strip()


def normalizar(bruto, origen):
    nivel = nivel_norm(pick(bruto, "nivel", "color", "coloralerta", "tipo_alerta",
                            "tipoalerta", "alerta") or pick(bruto, "titulo", "title") or "")
    titulo = pick(bruto, "titulo", "title", "nombre") or ""
    descripcion = pick(bruto, "descripcion", "detalle", "texto", "resumen") or ""
    if not nivel:
        nivel = nivel_norm(titulo + " " + descripcion)
    if not nivel:
        return None

    comunas = separar_comunas(pick(bruto, "comuna", "comunas", "nom_comuna") or "")
    region = region_canon(pick(bruto, "region", "nom_region") or "")
    estado_raw = slug(str(pick(bruto, "estado", "vigente", "activo", "status") or "vigente"))
    estado = "CANCELADA" if ("cancel" in estado_raw or estado_raw in ("0", "false", "no")) else "VIGENTE"
    if "se cancela" in slug(titulo):
        estado = "CANCELADA"
    ts = pick(bruto, "fecha", "fechahora", "fecha_creacion", "created", "ts") or ahora()
    url = pick(bruto, "url", "link", "enlace") or "https://senapred.cl/eventos/"

    return {
        "id": make_id(origen, nivel, titulo, region, ",".join(comunas), str(ts)[:10]),
        "nivel": nivel,
        "tipo": tipo_de(f"{titulo} {descripcion}"),
        "titulo": str(titulo).strip() or f"Alerta {nivel.title()}",
        "descripcion": str(descripcion).strip()[:600],
        "region": region,
        "comunas": comunas,
        "estado": estado,
        "ts": str(ts),
        "url": url,
        "origen": origen,
    }


# --------------------- FUENTE 1: AppSync GraphQL ------------------------------

def _cognito(target, body):
    """Llamadas SIN firma al mesón de pases de visitante (así lo permite AWS)."""
    r = requests.post(f"https://cognito-identity.{AWS_REGION}.amazonaws.com/",
                      timeout=TIMEOUT, data=json.dumps(body),
                      headers={"Content-Type": "application/x-amz-json-1.1",
                               "X-Amz-Target": f"AWSCognitoIdentityService.{target}"})
    r.raise_for_status()
    return r.json()


def credenciales_visitante():
    identity = COGNITO_IDENTITY_ID
    if not identity:
        identity = _cognito("GetId", {"IdentityPoolId": IDENTITY_POOL_ID})["IdentityId"]
    c = _cognito("GetCredentialsForIdentity", {"IdentityId": identity})["Credentials"]
    return c["AccessKeyId"], c["SecretKey"], c["SessionToken"]


def _hmac(key, msg):
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def firmar_sigv4(access_key, secret, token, body):
    """El sello matemático que AWS exige por petición (SigV4, servicio appsync)."""
    host = urlparse(APPSYNC_URL).netloc
    t = ahora_dt()
    amz_date = t.strftime("%Y%m%dT%H%M%SZ")
    fecha = t.strftime("%Y%m%d")
    ctype = "application/json; charset=UTF-8"

    canon_headers = (f"content-type:{ctype}\nhost:{host}\n"
                     f"x-amz-date:{amz_date}\nx-amz-security-token:{token}\n")
    firmados = "content-type;host;x-amz-date;x-amz-security-token"
    canonica = "\n".join(["POST", "/graphql", "", canon_headers, firmados,
                          hashlib.sha256(body.encode()).hexdigest()])
    alcance = f"{fecha}/{AWS_REGION}/appsync/aws4_request"
    a_firmar = "\n".join(["AWS4-HMAC-SHA256", amz_date, alcance,
                          hashlib.sha256(canonica.encode()).hexdigest()])
    k = _hmac(_hmac(_hmac(_hmac(("AWS4" + secret).encode(), fecha),
                          AWS_REGION), "appsync"), "aws4_request")
    firma = hmac.new(k, a_firmar.encode(), hashlib.sha256).hexdigest()

    return {
        "Content-Type": ctype,
        "X-Amz-Date": amz_date,
        "X-Amz-Security-Token": token,
        "Authorization": (f"AWS4-HMAC-SHA256 Credential={access_key}/{alcance}, "
                          f"SignedHeaders={firmados}, Signature={firma}"),
        "Origin": "https://www.senapred.cl",
        **UA,
    }


CONSULTA = """query AlertasByDate($type: String!, $fechaHora: ModelStringKeyConditionInput,
  $sortDirection: ModelSortDirection, $filter: ModelAlertaFilterInput,
  $limit: Int, $nextToken: String) {
  alertasByDate(type: $type, fechaHora: $fechaHora, sortDirection: $sortDirection,
    filter: $filter, limit: $limit, nextToken: $nextToken) {
    items { id titulo contenido fechaHora isActive isDeleted type urlAccess
            metaData createdAt isPrincipal
            variableRiesgo { nombre codigo } }
    nextToken
  }
}"""


def ventana_fechas():
    """Últimos 30 días: suficiente para vigentes + cancelaciones recientes."""
    hoy = ahora_dt()
    return [(hoy - dt.timedelta(days=30)).strftime("%Y-%m-%d"),
            hoy.strftime("%Y-%m-%dT23:59:59")]


def parse_item_graphql(item):
    """Un registro del AppSync de SENAPRED -> esquema común (o None)."""
    if item.get("isDeleted"):
        return None
    titulo = item.get("titulo", "")
    meta = {}
    try:
        meta = json.loads(item.get("metaData") or "{}")
    except Exception:
        pass
    bruto = {
        "titulo": titulo,
        "descripcion": sin_html(item.get("contenido", ""))[:600],
        "comuna": meta.get("comunas", ""),
        "region": (meta.get("regiones", "").split(",") or [""])[0],
        "estado": "vigente" if item.get("isActive", True) else "cancelada",
        "fecha": item.get("fechaHora") or item.get("createdAt") or "",
        "url": ("https://senapred.cl/"
                + ("alerta" if item.get("type") == "Alerta" else "evento")
                + "/" + item.get("urlAccess", "")),
    }
    a = normalizar(bruto, "senapred_web")
    if a:
        if a["tipo"] == "otro":  # respaldo: la variable de riesgo del propio SENAPRED
            vr = (item.get("variableRiesgo") or {}).get("nombre", "")
            a["tipo"] = tipo_de(vr) if tipo_de(vr) != "otro" else (vr.lower() or "otro")
        # regiones múltiples: una alerta regional puede abarcar varias
        regiones = [region_canon(r) for r in separar_comunas(meta.get("regiones", ""))]
        a["regiones_extra"] = [r for r in regiones[1:]] if len(regiones) > 1 else []
    return a


def fuente_web():
    if not IDENTITY_POOL_ID and not COGNITO_IDENTITY_ID:
        return [], "sin_configurar"
    try:
        ak, sk, tok = credenciales_visitante()
    except Exception as e:
        print(f"[web] ERROR credenciales Cognito: {e}", file=sys.stderr)
        return [], "error"

    alertas, ok = [], False
    for tipo in TIPOS_GRAPHQL:
        siguiente = None
        for _ in range(MAX_PAGINAS):
            body = json.dumps({"query": CONSULTA, "variables": {
                "type": tipo, "sortDirection": "DESC", "limit": LIMITE_GRAPHQL,
                "fechaHora": {"between": ventana_fechas()},
                "filter": {"isDeleted": {"eq": False}, "isPrincipal": {"eq": True}},
                "nextToken": siguiente}})
            try:
                r = requests.post(APPSYNC_URL, data=body, timeout=TIMEOUT,
                                  headers=firmar_sigv4(ak, sk, tok, body))
                r.raise_for_status()
                data = r.json()
                if data.get("errors"):
                    print(f"[web] GraphQL '{tipo}': {data['errors'][0].get('message')}",
                          file=sys.stderr)
                    break
                bloque = data["data"]["alertasByDate"]
                for it in bloque.get("items", []):
                    a = parse_item_graphql(it)
                    if a:
                        alertas.append(a)
                ok = True
                siguiente = bloque.get("nextToken")
                if not siguiente:
                    break
            except Exception as e:
                print(f"[web] ERROR consulta '{tipo}': {e}", file=sys.stderr)
                break
    return alertas, "ok" if ok else "error"


# --------------------- FUENTES 2 y 3 ------------------------------------------

def fuente_arcgis():
    if not ARCGIS_LAYERS:
        return [], "sin_configurar"
    alertas, ok = [], False
    for capa in ARCGIS_LAYERS:
        try:
            r = requests.get(capa.rstrip("/") + "/query", headers=UA, timeout=TIMEOUT,
                             params={"where": "1=1", "outFields": "*", "f": "json",
                                     "returnGeometry": "false", "resultRecordCount": 500})
            r.raise_for_status()
            for f in r.json().get("features", []):
                a = normalizar(f.get("attributes", {}), "senapred_arcgis")
                if a:
                    alertas.append(a)
            ok = True
        except Exception as e:
            print(f"[arcgis] ERROR en {capa}: {e}", file=sys.stderr)
    return alertas, "ok" if ok else "error"


def fuente_telegram():
    if not TELEGRAM_JSONL.exists():
        return [], "sin_configurar"
    alertas = []
    try:
        for linea in TELEGRAM_JSONL.read_text(encoding="utf-8").splitlines():
            if not linea.strip():
                continue
            reg = json.loads(linea)
            a = normalizar(reg, "telegram")
            if a:
                a["sae"] = bool(reg.get("sae"))
                alertas.append(a)
        return alertas, "ok"
    except Exception as e:
        print(f"[telegram] ERROR: {e}", file=sys.stderr)
        return [], "error"


# ----------------------------- MAIN ------------------------------------------

def main():
    web, est_web = fuente_web()
    arcgis, est_arcgis = fuente_arcgis()
    tg, est_tg = fuente_telegram()

    todas = {}
    for a in sorted(web + arcgis + tg, key=lambda x: x["ts"]):
        todas[a["id"]] = a
    alertas = sorted(todas.values(), key=lambda x: x["ts"], reverse=True)

    # Deduplicación entre fuentes: si dos registros describen el mismo hecho
    # (mismo nivel, mismo lugar, mismo día), queda uno. Manda la fuente web
    # (estado oficial + link al boletín); el sello SAE se hereda del gemelo.
    PRIORIDAD = {"senapred_web": 2, "telegram": 1, "senapred_arcgis": 0}
    canon, orden = {}, []
    for a in alertas:
        k = (a["nivel"], slug(a["region"]),
             ",".join(sorted(slug(c) for c in a["comunas"])), str(a["ts"])[:10])
        b = canon.get(k)
        if b is None:
            canon[k] = a
            orden.append(k)
        elif PRIORIDAD.get(a["origen"], 0) > PRIORIDAD.get(b["origen"], 0):
            if b.get("sae"):
                a["sae"] = True
            canon[k] = a
        elif a.get("sae"):
            b["sae"] = True
    duplicados = len(alertas) - len(orden)
    alertas = [canon[k] for k in orden]

    expiradas = 0
    for a in alertas:
        if a["origen"] in ("telegram", "senapred_web") and a["estado"] == "VIGENTE":
            edad = antiguedad_dias(a["ts"])
            if edad is not None and edad > VIGENCIA_DIAS:
                a["estado"] = "EXPIRADA"
                expiradas += 1

    vigentes = [a for a in alertas if a["estado"] == "VIGENTE"]

    SALIDA.write_text(json.dumps({
        "generado": ahora(),
        "fuente_oficial": "SENAPRED (senapred.cl) — sitio independiente, no oficial",
        "fuentes": {"web": est_web, "arcgis": est_arcgis, "telegram": est_tg},
        "total_vigentes": len(vigentes),
        "alertas": alertas[:500],
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    vistos = set()
    if HISTORICO.exists():
        for linea in HISTORICO.read_text(encoding="utf-8").splitlines():
            try:
                vistos.add(json.loads(linea)["id"])
            except Exception:
                pass
    with HISTORICO.open("a", encoding="utf-8") as f:
        nuevos = 0
        for a in alertas:
            if a["id"] not in vistos:
                f.write(json.dumps({**a, "registrado": ahora()}, ensure_ascii=False) + "\n")
                nuevos += 1

    print(f"OK {ahora()} | vigentes={len(vigentes)} expiradas={expiradas} "
          f"duplicados_fusionados={duplicados} total={len(alertas)} "
          f"nuevos_hist={nuevos} | web={est_web} arcgis={est_arcgis} telegram={est_tg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
