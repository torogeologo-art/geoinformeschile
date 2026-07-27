#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GeoInformesChile — generador de GeoInformes. (v3: eventos nombrados)

Dos productos:
  1. informe.json — el informe RODANTE del presente (ventana de los últimos
     DIAS_DEFECTO días), como siempre.
  2. informes/<id>.json — un informe CONGELADO por cada evento nombrado en
     eventos.json (curaduría de Diego: nombre y fechas). Se genera UNA vez e
     incluye las lluvias y vientos reales del período por región, desde el
     archivo histórico de Open-Meteo (modelo, referencial). Para regenerar
     un evento, se borra su archivo en informes/.

Determinista, sin LLM. Única dependencia: requests (para el clima de archivo).
"""

import datetime as dt
import json
import os
import pathlib
import time
import unicodedata
from collections import Counter, defaultdict

import requests

DIR = pathlib.Path(__file__).parent
HISTORICO = DIR / "historico.jsonl"
ALERTS = DIR / "alerts.json"
EVENTOS = DIR / "eventos.json"
CARPETA = DIR / "informes"
SALIDA_RODANTE = DIR / "informe.json"

EVENTO_NOMBRE = os.environ.get(
    "EVENTO_NOMBRE", "Situación actual — ventana de los últimos 10 días")
DIAS_DEFECTO = 10
PESO = {"ATP": 1, "AMARILLA": 2, "ROJA": 3}
BASURA_REGION = {"de", "del", "de la", "la", "los", "las", "el"}

CAPITALES = {
    "Arica y Parinacota": ("Arica", -18.48, -70.31), "Tarapacá": ("Iquique", -20.21, -70.15),
    "Antofagasta": ("Antofagasta", -23.65, -70.40), "Atacama": ("Copiapó", -27.37, -70.33),
    "Coquimbo": ("La Serena", -29.90, -71.25), "Valparaíso": ("Valparaíso", -33.05, -71.62),
    "Metropolitana de Santiago": ("Santiago", -33.45, -70.66),
    "Libertador General Bernardo O'Higgins": ("Rancagua", -34.17, -70.74),
    "Maule": ("Talca", -35.43, -71.65), "Ñuble": ("Chillán", -36.61, -72.10),
    "Biobío": ("Concepción", -36.83, -73.05), "La Araucanía": ("Temuco", -38.74, -72.59),
    "Los Ríos": ("Valdivia", -39.81, -73.25), "Los Lagos": ("Puerto Montt", -41.47, -72.94),
    "Aysén del General Carlos Ibáñez del Campo": ("Coyhaique", -45.57, -72.07),
    "Magallanes y de la Antártica Chilena": ("Punta Arenas", -53.16, -70.91),
}


def slug(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().split())


def region_limpia(r):
    t = slug(r)
    return "" if (len(t) < 4 or t in BASURA_REGION) else str(r).strip()


def ts_dt(ts):
    try:
        t = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None


def cargar_historico(desde, hasta):
    """Histórico filtrado a la ventana y deduplicado (web manda, SAE se hereda)."""
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
        if not t or t < desde or t > hasta or not a.get("nivel"):
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


def lugar_de(a):
    if a.get("comunas"):
        return ", ".join(a["comunas"][:3]) + (" (+)" if len(a["comunas"]) > 3 else "")
    return f"Región de {a['region']}" if a.get("region") else "Nivel nacional"


def construir_informe(nombre, desde, hasta, incluir_vigentes):
    """El cálculo completo de un GeoInforme para una ventana dada."""
    regs = cargar_historico(desde, hasta)

    por_nivel = Counter(a["nivel"] for a in regs)
    por_tipo = Counter(a.get("tipo", "otro") for a in regs)
    comunas = {slug(c) for a in regs for c in a.get("comunas", [])}
    regiones = {slug(a["region"]) for a in regs if a.get("region")}
    sae_total = sum(1 for a in regs if a.get("sae"))

    dias = defaultdict(lambda: {"ROJA": 0, "AMARILLA": 0, "ATP": 0, "sae": 0})
    for a in regs:
        d = a["_t"].strftime("%Y-%m-%d")
        dias[d][a["nivel"]] += 1
        if a.get("sae"):
            dias[d]["sae"] += 1
    serie = [{"fecha": d, **dias[d]} for d in sorted(dias)]

    por_lugar = defaultdict(list)
    for a in regs:
        por_lugar[(slug(a.get("region")),
                   ",".join(sorted(slug(c) for c in a.get("comunas", []))))].append(a)
    escalamientos = []
    for grupo in por_lugar.values():
        pasos, ult = [], None
        for a in grupo:
            if a["nivel"] != ult:
                pasos.append({"nivel": a["nivel"], "ts": a["_t"].strftime("%d-%m %H:%M"),
                              "estado": a.get("estado", "VIGENTE")})
                ult = a["nivel"]
        if any(PESO[pasos[i]["nivel"]] > PESO[pasos[i - 1]["nivel"]]
               for i in range(1, len(pasos))):
            escalamientos.append({
                "lugar": lugar_de(grupo[-1]), "region": grupo[-1].get("region", ""),
                "sae": any(a.get("sae") for a in grupo),
                "max": max(pasos, key=lambda p: PESO[p["nivel"]])["nivel"],
                "pasos": pasos[-5:]})
    escalamientos.sort(key=lambda e: (-PESO[e["max"]], -len(e["pasos"])))

    tipos_detalle = {}
    for a in regs:
        t = a.get("tipo", "otro")
        lk = (slug(a.get("region")),
              ",".join(sorted(slug(c) for c in a.get("comunas", []))))
        z = tipos_detalle.setdefault(t, {}).get(lk)
        if z is None or PESO[a["nivel"]] > PESO[z["nivel"]]:
            tipos_detalle[t][lk] = {
                "lugar": lugar_de(a), "region": a.get("region", ""), "nivel": a["nivel"],
                "sae": bool(a.get("sae")) or bool(z and z["sae"]),
                "ts": a["_t"].strftime("%d-%m"),
                "comuna_link": slug(a["comunas"][0]) if a.get("comunas") else "",
                "region_link": slug(a.get("region", ""))}
        elif a.get("sae"):
            z["sae"] = True
    tipos_detalle = {t: sorted((z for z in v.values()
                                if z["comuna_link"] or z["region_link"]),
                               key=lambda x: -PESO[x["nivel"]])[:20]
                     for t, v in tipos_detalle.items()}

    cronologia = [{"ts": a["_t"].strftime("%d-%m %H:%M"), "nivel": a["nivel"],
                   "lugar": lugar_de(a), "tipo": a.get("tipo", ""),
                   "sae": bool(a.get("sae")), "estado": a.get("estado", "VIGENTE"),
                   "url": a.get("url", "")} for a in regs[-14:]][::-1]

    vigentes = {"ROJA": 0, "AMARILLA": 0, "ATP": 0, "regiones_roja": []}
    if incluir_vigentes and ALERTS.exists():
        try:
            rojas = set()
            for a in json.loads(ALERTS.read_text(encoding="utf-8")).get("alertas", []):
                if a.get("estado") == "VIGENTE" and a.get("nivel") in vigentes:
                    vigentes[a["nivel"]] += 1
                    if a["nivel"] == "ROJA" and a.get("region"):
                        rojas.add(a["region"])
            vigentes["regiones_roja"] = sorted(rojas)
        except Exception:
            pass

    return {
        "generado": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "evento": {"nombre": nombre, "desde": desde.strftime("%d-%m-%Y"),
                   "hasta": hasta.strftime("%d-%m-%Y")},
        "totales": {"alertas": len(regs), "sae": sae_total, "comunas": len(comunas),
                    "regiones": len(regiones), "por_nivel": dict(por_nivel),
                    "por_tipo": por_tipo.most_common(8)},
        "tipos_detalle": {t: tipos_detalle.get(t, []) for t, _ in por_tipo.most_common(8)},
        "vigentes": vigentes,
        "serie": serie,
        "escalamientos": escalamientos[:12],
        "cronologia": cronologia,
        "nota": ("Informe generado automáticamente por GeoInformesChile a partir de "
                 "los registros públicos de SENAPRED. Documento referencial, no oficial."),
    }, {r for r in (region_limpia(x.get("region")) for x in regs) if r}


def clima_de_archivo(regiones, desde, hasta):
    """Lluvia y viento horarios REALES del período, por capital regional,
    desde el archivo histórico de Open-Meteo (modelo ERA5, referencial)."""
    out = {}
    for r in sorted(regiones):
        if r not in CAPITALES:
            continue
        ciudad, lat, lon = CAPITALES[r]
        try:
            resp = requests.get("https://archive-api.open-meteo.com/v1/archive",
                                timeout=25, params={
                                    "latitude": lat, "longitude": lon,
                                    "start_date": desde, "end_date": hasta,
                                    "hourly": "precipitation,wind_speed_10m",
                                    "timezone": "America/Santiago"})
            resp.raise_for_status()
            h = resp.json().get("hourly", {})
            if h.get("time"):
                out[r] = {"ciudad": ciudad, "time": h["time"],
                          "precipitation": h["precipitation"],
                          "wind_speed_10m": h["wind_speed_10m"]}
            time.sleep(0.4)
        except Exception as e:
            print(f"[clima-archivo] {r}: {e}")
    return out


def main():
    tz = dt.timezone.utc
    hoy = dt.datetime.now(tz)

    # 1) Informe rodante del presente (comportamiento de siempre)
    v = os.environ.get("EVENTO_DESDE", "").strip()
    desde = ts_dt(v) or (hoy - dt.timedelta(days=DIAS_DEFECTO))
    informe, _ = construir_informe(EVENTO_NOMBRE, desde, hoy, incluir_vigentes=True)
    SALIDA_RODANTE.write_text(json.dumps(informe, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    print(f"OK informe | registros={informe['totales']['alertas']} "
          f"escalamientos={len(informe['escalamientos'])} dias={len(informe['serie'])} "
          f"sae={informe['totales']['sae']}")

    # 2) Eventos nombrados: congelar los que falten
    if not EVENTOS.exists():
        return 0
    CARPETA.mkdir(exist_ok=True)
    for ev in json.loads(EVENTOS.read_text(encoding="utf-8")).get("eventos", []):
        destino = CARPETA / f"{ev['id']}.json"
        if destino.exists():
            continue  # congelado: no se recalcula (borrar el archivo para regenerar)
        d0 = dt.datetime.fromisoformat(ev["desde"]).replace(tzinfo=tz)
        d1 = dt.datetime.fromisoformat(ev["hasta"]).replace(hour=23, minute=59, tzinfo=tz)
        inf, regiones = construir_informe(ev["nombre"], d0, d1, incluir_vigentes=False)
        inf["archivado"] = True
        if d1 < hoy - dt.timedelta(days=2):  # el archivo meteorológico ya está firme
            inf["clima_archivo"] = clima_de_archivo(regiones, ev["desde"], ev["hasta"])
        destino.write_text(json.dumps(inf, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"OK evento congelado | {ev['id']} registros={inf['totales']['alertas']} "
              f"clima_regiones={len(inf.get('clima_archivo', {}))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
