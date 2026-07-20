#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GeoInformesChile — módulo media: videos de YouTube por zona en alerta.

Única API legal de geo-video que queda viva. Estrategia:
  - Barrido solo para zonas en ROJA vigente (cuota: cada search cuesta 100 de
    las 10.000 unidades diarias => presupuesto duro MAX_BUSQUEDAS).
  - "Buscador inteligente": expande queries con los topónimos reales del
    boletín (río X, quebrada Y, sector Z) además del nombre de la comuna,
    filtra por publishedAfter (nada de videos reciclados de otros años) y
    puntúa por recencia + topónimo + live.
  - Re-ranking LLM opcional si hay ANTHROPIC_API_KEY (si no, heurística sola).

Salida: media.json  ->  { zonas: { "<slug comuna>": {comuna, videos:[...] } } }
Todo video sale marcado verificado:false. La verificación humana/visual es fase 3.
"""

import datetime as dt
import json
import os
import pathlib
import re
import sys
import unicodedata

import requests

DIR = pathlib.Path(__file__).parent
ALERTS = DIR / "alerts.json"
SALIDA = DIR / "media.json"

YT_URL = "https://www.googleapis.com/youtube/v3/search"
MAX_BUSQUEDAS = int(os.environ.get("MAX_BUSQUEDAS", "40"))   # tope duro por corrida
MAX_VIDEOS_ZONA = 8
INCLUIR_AMARILLAS = os.environ.get("INCLUIR_AMARILLAS", "0") == "1"
VENTANA_HRS_DEFECTO = 48

RE_TOPONIMO = re.compile(
    r"(?:r[ií]o|quebrada|estero|sector|poblaci[oó]n|cerro)\s+"
    r"([A-ZÁÉÍÓÚÑ][\wáéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][\wáéíóúñ]+)?)")


def cargar_env():
    p = DIR / ".env"
    if p.exists():
        for linea in p.read_text(encoding="utf-8").splitlines():
            linea = linea.strip()
            if linea and not linea.startswith("#") and "=" in linea:
                k, _, v = linea.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


def slug(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().split())


def desde_iso():
    v = os.environ.get("EVENTO_DESDE", "").strip()
    if v:
        return v
    t = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=VENTANA_HRS_DEFECTO)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def yt_search(key, q, desde, live=False):
    params = {
        "key": key, "part": "snippet", "type": "video", "maxResults": 10,
        "q": q, "order": "date", "publishedAfter": desde,
        "regionCode": "CL", "relevanceLanguage": "es",
    }
    if live:
        params["eventType"] = "live"
        params.pop("order")  # live: relevancia por defecto
    r = requests.get(YT_URL, params=params, timeout=20)
    r.raise_for_status()
    return r.json().get("items", [])


def puntuar(item, comuna, toponimos, live):
    sn = item.get("snippet", {})
    titulo = slug(sn.get("title", "") + " " + sn.get("description", ""))
    score = 4.0 if live else 0.0
    if slug(comuna) and slug(comuna) in titulo:
        score += 3
    score += sum(2 for t in toponimos if slug(t) in titulo)
    try:
        pub = dt.datetime.fromisoformat(sn.get("publishedAt", "").replace("Z", "+00:00"))
        horas = (dt.datetime.now(dt.timezone.utc) - pub).total_seconds() / 3600
        score += max(0.0, 3.0 - horas / 12.0)  # recencia: 3 pts frescos, 0 a las 36 h
    except Exception:
        pass
    return round(score, 2)


def rerank_llm(candidatos, contexto):
    """Opcional: Claude bota falsos positivos (mismo topónimo en otra región,
    videos reciclados, clickbait). Devuelve los índices que sobreviven, en orden."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or not candidatos:
        return None
    try:
        import anthropic
        listado = "\n".join(f'{i}: {c["titulo"]} | canal {c["canal"]} | {c["publicado"]}'
                            for i, c in enumerate(candidatos))
        msg = anthropic.Anthropic(api_key=key).messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=300,
            messages=[{"role": "user", "content":
                f"Contexto de emergencia real en Chile: {contexto}.\n"
                f"Candidatos de video:\n{listado}\n\n"
                "Responde SOLO un array JSON con los índices de videos que muy "
                "probablemente muestren ESTA zona y ESTE evento, ordenados de mejor "
                "a peor. Excluye otros lugares, otros años y clickbait. Sin texto extra."}])
        idx = json.loads(msg.content[0].text.strip())
        return [i for i in idx if isinstance(i, int) and 0 <= i < len(candidatos)]
    except Exception as e:
        print(f"[llm] omitido: {e}", file=sys.stderr)
        return None


def main():
    cargar_env()
    key = os.environ.get("YT_API_KEY")
    if not key:
        print("Falta YT_API_KEY (Google Cloud Console > YouTube Data API v3).",
              file=sys.stderr)
        return 1
    if not ALERTS.exists():
        print("No existe alerts.json: corre primero colector_senapred.py.", file=sys.stderr)
        return 1

    data = json.loads(ALERTS.read_text(encoding="utf-8"))
    niveles = {"ROJA"} | ({"AMARILLA"} if INCLUIR_AMARILLAS else set())
    objetivo = [a for a in data.get("alertas", [])
                if a["estado"] == "VIGENTE" and a["nivel"] in niveles]

    desde = desde_iso()
    zonas, busquedas = {}, 0

    for a in objetivo:
        toponimos = RE_TOPONIMO.findall(a.get("descripcion", "") + " " + a.get("titulo", ""))
        lugares = a["comunas"] or ([a["region"]] if a["region"] else [])
        for comuna in lugares:
            sl = slug(comuna)
            if not sl or sl in zonas:
                continue
            if busquedas + 2 > MAX_BUSQUEDAS:
                print(f"[cuota] tope de {MAX_BUSQUEDAS} búsquedas alcanzado.", file=sys.stderr)
                break

            vistos, candidatos = set(), []
            consultas = [(f"{comuna} Chile", True),
                         (f"{comuna} {a['tipo']} hoy", False)]
            for q, live in consultas:
                try:
                    items = yt_search(key, q, desde, live=live)
                    busquedas += 1
                except Exception as e:
                    print(f"[yt] ERROR '{q}': {e}", file=sys.stderr)
                    continue
                for it in items:
                    vid = it.get("id", {}).get("videoId")
                    if not vid or vid in vistos:
                        continue
                    vistos.add(vid)
                    sn = it["snippet"]
                    es_live = live or sn.get("liveBroadcastContent") == "live"
                    candidatos.append({
                        "id": vid,
                        "url": f"https://www.youtube.com/watch?v={vid}",
                        "titulo": sn.get("title", ""),
                        "canal": sn.get("channelTitle", ""),
                        "publicado": sn.get("publishedAt", ""),
                        "live": es_live,
                        "score": puntuar(it, comuna, toponimos, es_live),
                        "verificado": False,
                    })

            candidatos.sort(key=lambda c: c["score"], reverse=True)
            orden = rerank_llm(candidatos[:12],
                               f"{a['nivel']} por {a['tipo']} en {comuna}, {a['region']}")
            if orden is not None:
                candidatos = [candidatos[i] for i in orden]
            if candidatos:
                zonas[sl] = {"comuna": comuna, "region": a["region"],
                             "alerta": a["nivel"], "videos": candidatos[:MAX_VIDEOS_ZONA]}

    SALIDA.write_text(json.dumps({
        "generado": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "desde": desde,
        "nota": "Videos no verificados, hallados por búsqueda pública. No es material oficial.",
        "zonas": zonas,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"OK media | zonas={len(zonas)} busquedas={busquedas}/{MAX_BUSQUEDAS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
