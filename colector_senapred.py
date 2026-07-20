#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GeoInformesChile — colector de alertas SENAPRED. (v2)

Cambios v2 (tras probar contra boletines reales del canal de Telegram):
  - Comunas múltiples en un texto ("Longaví y Linares") se separan bien.
  - Red de seguridad de vigencia: una alerta de origen Telegram con más de
    VIGENCIA_DIAS sin renovarse pasa a estado EXPIRADA y deja de pintarse.
    El mapa nunca puede mostrar pasado como presente.

Fuentes (en orden de confianza):
  1. Endpoint JSON no documentado que alimenta senapred.cl/eventos  -> WEB_ENDPOINT
  2. Capas ArcGIS del dashboard público "ALERTAS SENAPRED"          -> ARCGIS_LAYERS
  3. Registros parseados del canal de Telegram (telegram_sae.py)    -> telegram_alertas.jsonl

Salidas:
  alerts.json     estado completo para el frontend (se escribe SIEMPRE)
  historico.jsonl registro append-only para informes y timeline
"""

import datetime as dt
import hashlib
import json
import pathlib
import re
import sys
import unicodedata

import requests

# ----------------------------- CONFIG ---------------------------------------

# TODO(1): pegar aquí el endpoint que alimenta https://www.senapred.cl/eventos
# (DevTools > Network > Fetch/XHR > recargar; la llamada que devuelve la lista).
WEB_ENDPOINT = ""

# TODO(2): URLs de capas FeatureServer del dashboard "ALERTAS SENAPRED".
# Sacarlas de: https://www.arcgis.com/sharing/rest/content/items/bdc345e01e324af490800634d0f0e3a5/data?f=json
# (toda "url" que termine en /FeatureServer/<n>).
ARCGIS_LAYERS = []

VIGENCIA_DIAS = 7   # alertas de Telegram sin renovar en este plazo -> EXPIRADA
TIMEOUT = 25
DIR = pathlib.Path(__file__).parent
SALIDA = DIR / "alerts.json"
HISTORICO = DIR / "historico.jsonl"
TELEGRAM_JSONL = DIR / "telegram_alertas.jsonl"

UA = {"User-Agent": "GeoInformesChile/0.1 (colector de utilidad publica; fuente: SENAPRED)"}

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
    """'Longaví y Linares' | 'A, B; C' -> ['Longaví','Linares'] / ['A','B','C']"""
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


def normalizar(bruto, origen):
    nivel = nivel_norm(pick(bruto, "nivel", "color", "coloralerta", "tipo_alerta",
                            "tipoalerta", "alerta") or pick(bruto, "titulo", "title") or "")
    titulo = pick(bruto, "titulo", "title", "nombre", "descripcion_corta") or ""
    descripcion = pick(bruto, "descripcion", "detalle", "texto", "resumen", "description") or ""
    if not nivel:
        nivel = nivel_norm(titulo + " " + descripcion)
    if not nivel:
        return None

    comunas = separar_comunas(pick(bruto, "comuna", "comunas", "nom_comuna") or "")
    region = pick(bruto, "region", "nom_region", "region_nombre") or ""
    estado_raw = slug(str(pick(bruto, "estado", "vigente", "activo", "status") or "vigente"))
    estado = "CANCELADA" if ("cancel" in estado_raw or estado_raw in ("0", "false", "no")) else "VIGENTE"
    ts = pick(bruto, "fecha", "fecha_creacion", "fechaestado", "created", "date", "ts") or ahora()
    url = pick(bruto, "url", "link", "enlace", "url_boletin") or "https://senapred.cl/eventos/"
    texto_total = f"{titulo} {descripcion}"

    return {
        "id": make_id(origen, nivel, titulo, region, ",".join(comunas), str(ts)[:10]),
        "nivel": nivel,
        "tipo": tipo_de(texto_total),
        "titulo": titulo.strip() or f"Alerta {nivel.title()}",
        "descripcion": str(descripcion).strip()[:600],
        "region": str(region).strip(),
        "comunas": comunas,
        "estado": estado,
        "ts": str(ts),
        "url": url,
        "origen": origen,
    }


# ----------------------------- FUENTES ---------------------------------------

def _primera_lista(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for v in data.values():
            r = _primera_lista(v)
            if isinstance(r, list) and r and isinstance(r[0], dict):
                return r
    return []


def fuente_web():
    if not WEB_ENDPOINT:
        return [], "sin_configurar"
    try:
        r = requests.get(WEB_ENDPOINT, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        registros = _primera_lista(r.json())
        alertas = [a for a in (normalizar(x, "senapred_web") for x in registros) if a]
        if registros and not alertas:
            print("[web] 0 alertas mapeadas. Llaves del primer registro:",
                  sorted(registros[0].keys()), file=sys.stderr)
        return alertas, "ok"
    except Exception as e:
        print(f"[web] ERROR: {e}", file=sys.stderr)
        return [], "error"


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

    # Merge: gana el registro más nuevo por id.
    todas = {}
    for a in sorted(web + arcgis + tg, key=lambda x: x["ts"]):
        todas[a["id"]] = a
    alertas = sorted(todas.values(), key=lambda x: x["ts"], reverse=True)

    # Red de seguridad: alertas de Telegram viejas sin renovar -> EXPIRADA.
    expiradas = 0
    for a in alertas:
        if a["origen"] == "telegram" and a["estado"] == "VIGENTE":
            edad = antiguedad_dias(a["ts"])
            if edad is not None and edad > VIGENCIA_DIAS:
                a["estado"] = "EXPIRADA"
                expiradas += 1

    vigentes = [a for a in alertas if a["estado"] == "VIGENTE"]

    salida = {
        "generado": ahora(),
        "fuente_oficial": "SENAPRED (senapred.cl) — sitio independiente, no oficial",
        "fuentes": {"web": est_web, "arcgis": est_arcgis, "telegram": est_tg},
        "total_vigentes": len(vigentes),
        "alertas": alertas[:500],
    }
    SALIDA.write_text(json.dumps(salida, ensure_ascii=False, indent=1), encoding="utf-8")

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

    print(f"OK {ahora()} | vigentes={len(vigentes)} expiradas={expiradas} total={len(alertas)} "
          f"nuevos_hist={nuevos} | web={est_web} arcgis={est_arcgis} telegram={est_tg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
