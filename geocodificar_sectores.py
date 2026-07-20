#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GeoInformesChile — geocodificador de sectores. (Parte 2, capa 1)

Los boletines nombran lugares precisos ("río Elqui", "quebrada El Romeral",
"sector Alfalfares") pero el mapa solo pintaba comunas. Este script:
  1. Lee alerts.json y extrae los topónimos de las alertas ROJAS y AMARILLAS
     vigentes (regex de río/quebrada/estero/sector/etc.).
  2. Los geocodifica con Nominatim (OpenStreetMap, gratis) usando la comuna o
     región como contexto: "Río Elqui, La Serena, Chile".
  3. Guarda los puntos en sectores.json para la capa de pines del mapa.

Respeto por Nominatim: máx 1 consulta/segundo, User-Agent identificado, y un
caché permanente (geocache.json) para que en régimen las corridas hagan CERO
consultas nuevas. Tope duro de consultas nuevas por corrida.

Expectativa honesta: los nombres informales calzan un 50–70%. Lo que no
calza queda en el caché como nulo y no se muestra — mejor sin pin que con
un pin inventado.
"""

import datetime as dt
import json
import pathlib
import re
import sys
import time
import unicodedata

import requests

DIR = pathlib.Path(__file__).parent
ALERTS = DIR / "alerts.json"
CACHE = DIR / "geocache.json"
SALIDA = DIR / "sectores.json"

MAX_CONSULTAS_NUEVAS = 15
NIVELES = {"ROJA", "AMARILLA"}
UA = {"User-Agent": "GeoInformesChile/1.0 (mapa ciudadano de alertas; contacto: geoinformeschile@gmail.com)"}

RE_TOPONIMO = re.compile(
    r"(?:r[ií]o|quebrada|estero|laguna|sector(?:es)?|poblaci[oó]n|cerro|puente|caleta)\s+"
    r"([A-ZÁÉÍÓÚÑ][\wáéíóúñ.]+(?:\s+(?:de\s+|del\s+|la\s+|el\s+|los\s+|las\s+)?"
    r"[A-ZÁÉÍÓÚÑ][\wáéíóúñ.]+){0,2})")


def slug(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().split())


def frase_completa(m):
    """Devuelve 'río Elqui' (con su palabra clave), no solo 'Elqui'."""
    return m.group(0).strip()


def geocodificar(consulta):
    r = requests.get("https://nominatim.openstreetmap.org/search", headers=UA,
                     timeout=20, params={"q": consulta, "format": "json",
                                         "limit": 1, "countrycodes": "cl"})
    r.raise_for_status()
    res = r.json()
    if not res:
        return None
    return {"lat": round(float(res[0]["lat"]), 5),
            "lon": round(float(res[0]["lon"]), 5)}


def main():
    if not ALERTS.exists():
        print("No existe alerts.json: corre primero colector_senapred.py.", file=sys.stderr)
        return 1
    data = json.loads(ALERTS.read_text(encoding="utf-8"))
    cache = {}
    if CACHE.exists():
        try:
            cache = json.loads(CACHE.read_text(encoding="utf-8"))
        except Exception:
            pass

    sectores, vistos, nuevas = [], set(), 0
    for a in data.get("alertas", []):
        if a.get("estado") != "VIGENTE" or a.get("nivel") not in NIVELES:
            continue
        texto = f'{a.get("titulo", "")} {a.get("descripcion", "")}'
        contexto = (a["comunas"][0] if a.get("comunas") else a.get("region", "")) or "Chile"
        for m in RE_TOPONIMO.finditer(texto):
            nombre = frase_completa(m)
            k = f"{slug(nombre)}|{slug(contexto)}"
            if k in vistos:
                continue
            vistos.add(k)

            if k not in cache:
                if nuevas >= MAX_CONSULTAS_NUEVAS:
                    continue  # queda para la próxima corrida
                consulta = f"{nombre}, {contexto}, Chile"
                try:
                    cache[k] = geocodificar(consulta)
                    nuevas += 1
                    time.sleep(1.1)  # política de Nominatim: 1 req/s
                except Exception as e:
                    print(f"[geo] ERROR '{consulta}': {e}", file=sys.stderr)
                    continue

            pto = cache.get(k)
            if pto:
                sectores.append({
                    "nombre": nombre, "lat": pto["lat"], "lon": pto["lon"],
                    "nivel": a["nivel"], "tipo": a.get("tipo", ""),
                    "sae": bool(a.get("sae")),
                    "lugar": contexto, "url": a.get("url", ""),
                    "ts": str(a.get("ts", ""))[:16],
                })

    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")
    SALIDA.write_text(json.dumps({
        "generado": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "nota": "Puntos geocodificados automáticamente desde los boletines (referenciales).",
        "sectores": sectores,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"OK sectores | pines={len(sectores)} consultas_nuevas={nuevas} cache={len(cache)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
