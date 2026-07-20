#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GeoInformesChile — módulo media: videos de YouTube por zona en alerta. (v3)

Cambios v3 (tras la primera cosecha real en producción):
  - Verifica con videos.list si cada video PERMITE embed (status.embeddable);
    los bloqueados por su dueño se marcan y el sitio los abre en YouTube en
    vez de mostrar un reproductor roto. Costo: 1 unidad por lote de 50.
  - Penaliza candidatos cuyo título nombra OTRA comuna de Chile y no la zona
    objetivo (el caso "Viña del Mar" apareciendo bajo La Serena).
  - Confirma el estado "en vivo" real desde videos.list.

Estrategia general: barrido solo para zonas en ROJA vigente, queries
expandidas con topónimos del boletín, publishedAfter contra videos
reciclados, presupuesto duro de cuota, re-ranking LLM opcional
(ANTHROPIC_API_KEY). Todo sale marcado verificado:false.
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
COMUNAS_GEO = DIR / "comunas.geojson"
SALIDA = DIR / "media.json"

YT_SEARCH = "https://www.googleapis.com/youtube/v3/search"
YT_VIDEOS = "https://www.googleapis.com/youtube/v3/videos"
MAX_BUSQUEDAS = int(os.environ.get("MAX_BUSQUEDAS", "40"))
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


def cargar_comunas_chile():
    """Set de nombres de comunas (slug) para detectar 'otra comuna' en títulos."""
    try:
        geo = json.loads(COMUNAS_GEO.read_text(encoding="utf-8"))
        out = set()
        for f in geo.get("features", []):
            p = f.get("properties", {})
            n = p.get("COMUNA") or p.get("Comuna") or p.get("NOM_COMUNA") or ""
            s = slug(n)
            if len(s) >= 4:
                out.add(s)
        return out
    except Exception:
        return set()


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
        params.pop("order")
    r = requests.get(YT_SEARCH, params=params, timeout=20)
    r.raise_for_status()
    return r.json().get("items", [])


def yt_estado_videos(key, ids):
    """videos.list en lotes de 50: embeddable + confirmación de live. 1 unidad/lote."""
    info = {}
    for i in range(0, len(ids), 50):
        lote = ids[i:i + 50]
        try:
            r = requests.get(YT_VIDEOS, timeout=20, params={
                "key": key, "part": "status,snippet", "id": ",".join(lote)})
            r.raise_for_status()
            for it in r.json().get("items", []):
                info[it["id"]] = {
                    "embeddable": bool(it.get("status", {}).get("embeddable", True)),
                    "live": it.get("snippet", {}).get("liveBroadcastContent") == "live",
                }
        except Exception as e:
            print(f"[yt] videos.list falló (se asume embebible): {e}", file=sys.stderr)
    return info


def puntuar(item, comuna, toponimos, live, otras_comunas):
    sn = item.get("snippet", {})
    titulo = slug(sn.get("title", "") + " " + sn.get("description", ""))
    pad = f" {titulo} "
    objetivo = slug(comuna)
    score = 4.0 if live else 0.0
    if objetivo and objetivo in titulo:
        score += 3
    score += sum(2 for t in toponimos if slug(t) in titulo)
    if objetivo and objetivo not in titulo:
        ajenas = sum(1 for c in otras_comunas
                     if c != objetivo and f" {c} " in pad)
        score -= 4 * min(ajenas, 2)   # nombra otra comuna y no la nuestra
    try:
        pub = dt.datetime.fromisoformat(sn.get("publishedAt", "").replace("Z", "+00:00"))
        horas = (dt.datetime.now(dt.timezone.utc) - pub).total_seconds() / 3600
        score += max(0.0, 3.0 - horas / 12.0)
    except Exception:
        pass
    return round(score, 2)


def rerank_llm(candidatos, contexto):
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

    otras_comunas = cargar_comunas_chile()
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
                        "score": puntuar(it, comuna, toponimos, es_live, otras_comunas),
                        "verificado": False,
                        "embeddable": True,
                    })

            candidatos.sort(key=lambda c: c["score"], reverse=True)
            candidatos = candidatos[:12]

            # Estado real: ¿permite embed? ¿sigue en vivo?
            info = yt_estado_videos(key, [c["id"] for c in candidatos])
            for c in candidatos:
                if c["id"] in info:
                    c["embeddable"] = info[c["id"]]["embeddable"]
                    c["live"] = c["live"] or info[c["id"]]["live"]

            orden = rerank_llm(candidatos,
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
