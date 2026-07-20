#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GeoInformesChile — generador del informe automático del evento.

Convierte historico.jsonl (todo lo que el robot ha registrado) + alerts.json
(el estado vigente) en informe.json: la materia prima del GeoInforme que
renderiza informe.html. Determinista, sin LLM, sin dependencias.

Qué calcula:
  - Totales del evento (alertas, SAE, comunas y regiones afectadas, por nivel/tipo).
  - Serie diaria de alertas nuevas por nivel + SAE (para el gráfico).
  - Escalamientos: lugares que subieron de nivel (ATP -> Amarilla -> Roja).
  - Cronología reciente con links a los boletines oficiales.

Configuración: EVENTO_NOMBRE y EVENTO_DESDE editables aquí o por variables
de entorno del robot. Por defecto: ventana de los últimos 10 días.
"""

import datetime as dt
import json
import os
import pathlib
import unicodedata
from collections import Counter, defaultdict

DIR = pathlib.Path(__file__).parent
HISTORICO = DIR / "historico.jsonl"
ALERTS = DIR / "alerts.json"
SALIDA = DIR / "informe.json"

EVENTO_NOMBRE = os.environ.get(
    "EVENTO_NOMBRE", "Sistema frontal — evento meteorológico en monitoreo")
DIAS_DEFECTO = 10
PESO = {"ATP": 1, "AMARILLA": 2, "ROJA": 3}


def slug(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().split())


def ts_dt(ts):
    try:
        t = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None


def desde_evento():
    v = os.environ.get("EVENTO_DESDE", "").strip()
    if v:
        d = ts_dt(v)
        if d:
            return d
    return dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=DIAS_DEFECTO)


def cargar_historico(desde):
    """Lee el histórico, filtra al evento y deduplica entre fuentes
    (mismo criterio del colector: nivel + lugar + día; manda la web, SAE se hereda)."""
    if not HISTORICO.exists():
        return []
    prioridad = {"senapred_web": 2, "telegram": 1, "senapred_arcgis": 0}
    canon, orden = {}, []
    for linea in HISTORICO.read_text(encoding="utf-8").splitlines():
        try:
            a = json.loads(linea)
        except Exception:
            continue
        t = ts_dt(a.get("ts"))
        if not t or t < desde or not a.get("nivel"):
            continue
        a["_t"] = t
        a["region"] = region_limpia(a.get("region"))
        k = (a["nivel"], slug(a.get("region")),
             ",".join(sorted(slug(c) for c in a.get("comunas", []))), str(a["ts"])[:10])
        b = canon.get(k)
        if b is None:
            canon[k] = a
            orden.append(k)
        elif prioridad.get(a.get("origen"), 0) > prioridad.get(b.get("origen"), 0):
            if b.get("sae"):
                a["sae"] = True
            canon[k] = a
        elif a.get("sae"):
            b["sae"] = True
    regs = [canon[k] for k in orden]
    regs.sort(key=lambda x: x["_t"])
    return regs


BASURA_REGION = {"de", "del", "de la", "la", "los", "las", "el"}


def region_limpia(r):
    """Sanea registros legados con capturas basura ('de')."""
    t = slug(r)
    return "" if (len(t) < 4 or t in BASURA_REGION) else str(r).strip()


def lugar_de(a):
    if a.get("comunas"):
        return ", ".join(a["comunas"][:3]) + (" (+)" if len(a["comunas"]) > 3 else "")
    return f"Región de {a['region']}" if a.get("region") else "Nivel nacional"


def main():
    desde = desde_evento()
    hoy = dt.datetime.now(dt.timezone.utc)
    regs = cargar_historico(desde)

    # ---- totales y desgloses -------------------------------------------------
    por_nivel = Counter(a["nivel"] for a in regs)
    por_tipo = Counter(a.get("tipo", "otro") for a in regs)
    comunas = {slug(c) for a in regs for c in a.get("comunas", [])}
    regiones = {slug(a["region"]) for a in regs if a.get("region")}
    sae_total = sum(1 for a in regs if a.get("sae"))

    # ---- serie diaria (para el gráfico) -------------------------------------
    dias = defaultdict(lambda: {"ROJA": 0, "AMARILLA": 0, "ATP": 0, "sae": 0})
    for a in regs:
        d = a["_t"].strftime("%Y-%m-%d")
        dias[d][a["nivel"]] += 1
        if a.get("sae"):
            dias[d]["sae"] += 1
    serie = [{"fecha": d, **dias[d]} for d in sorted(dias)]

    # ---- escalamientos por lugar --------------------------------------------
    por_lugar = defaultdict(list)
    for a in regs:
        k = (slug(a.get("region")),
             ",".join(sorted(slug(c) for c in a.get("comunas", []))))
        por_lugar[k].append(a)
    escalamientos = []
    for grupo in por_lugar.values():
        pasos, ult = [], None
        for a in grupo:  # ya vienen en orden temporal
            if a["nivel"] != ult:
                pasos.append({"nivel": a["nivel"],
                              "ts": a["_t"].strftime("%d-%m %H:%M"),
                              "estado": a.get("estado", "VIGENTE")})
                ult = a["nivel"]
        subio = any(PESO[pasos[i]["nivel"]] > PESO[pasos[i - 1]["nivel"]]
                    for i in range(1, len(pasos)))
        if subio:
            escalamientos.append({
                "lugar": lugar_de(grupo[-1]),
                "region": grupo[-1].get("region", ""),
                "sae": any(a.get("sae") for a in grupo),
                "max": max(pasos, key=lambda p: PESO[p["nivel"]])["nivel"],
                "pasos": pasos[-5:],
            })
    escalamientos.sort(key=lambda e: (-PESO[e["max"]], -len(e["pasos"])))

    # ---- desglose de zonas por tipo (para los chips clickeables) -------------
    tipos_detalle = {}
    for a in regs:
        t = a.get("tipo", "otro")
        lk = (slug(a.get("region")),
              ",".join(sorted(slug(c) for c in a.get("comunas", []))))
        z = tipos_detalle.setdefault(t, {}).get(lk)
        if z is None or PESO[a["nivel"]] > PESO[z["nivel"]]:
            tipos_detalle[t][lk] = {
                "lugar": lugar_de(a), "region": a.get("region", ""),
                "nivel": a["nivel"],
                "sae": bool(a.get("sae")) or bool(z and z["sae"]),
                "ts": a["_t"].strftime("%d-%m"),
                "comuna_link": slug(a["comunas"][0]) if a.get("comunas") else "",
                "region_link": slug(a.get("region", "")),
            }
        elif a.get("sae"):
            z["sae"] = True
    tipos_detalle = {t: sorted((z for z in v.values()
                                if z["comuna_link"] or z["region_link"]),
                               key=lambda x: -PESO[x["nivel"]])[:20]
                     for t, v in tipos_detalle.items()}

    # ---- cronología reciente -------------------------------------------------
    cronologia = [{
        "ts": a["_t"].strftime("%d-%m %H:%M"),
        "nivel": a["nivel"],
        "lugar": lugar_de(a),
        "tipo": a.get("tipo", ""),
        "sae": bool(a.get("sae")),
        "estado": a.get("estado", "VIGENTE"),
        "url": a.get("url", ""),
    } for a in regs[-14:]][::-1]

    # ---- estado vigente ahora (desde alerts.json) ---------------------------
    vigentes = {"ROJA": 0, "AMARILLA": 0, "ATP": 0, "regiones_roja": []}
    try:
        d = json.loads(ALERTS.read_text(encoding="utf-8"))
        rojas = set()
        for a in d.get("alertas", []):
            if a.get("estado") == "VIGENTE" and a.get("nivel") in vigentes:
                vigentes[a["nivel"]] += 1
                if a["nivel"] == "ROJA" and a.get("region"):
                    rojas.add(a["region"])
        vigentes["regiones_roja"] = sorted(rojas)
    except Exception:
        pass

    SALIDA.write_text(json.dumps({
        "generado": hoy.isoformat(timespec="seconds"),
        "evento": {"nombre": EVENTO_NOMBRE,
                   "desde": desde.strftime("%d-%m-%Y"),
                   "hasta": hoy.strftime("%d-%m-%Y")},
        "totales": {"alertas": len(regs), "sae": sae_total,
                    "comunas": len(comunas), "regiones": len(regiones),
                    "por_nivel": dict(por_nivel),
                    "por_tipo": por_tipo.most_common(8)},
        "tipos_detalle": {t: tipos_detalle.get(t, [])
                          for t, _ in por_tipo.most_common(8)},
        "vigentes": vigentes,
        "serie": serie,
        "escalamientos": escalamientos[:12],
        "cronologia": cronologia,
        "nota": ("Informe generado automáticamente por GeoInformesChile a partir de "
                 "los registros públicos de SENAPRED. Documento referencial, no oficial."),
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"OK informe | registros={len(regs)} escalamientos={len(escalamientos)} "
          f"dias={len(serie)} sae={sae_total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
