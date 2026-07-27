#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GeoInformesChile — compilador de noticias por zona. (Memoria, capa 1)

Convierte el sitio de "mapa del evento" en "memoria del territorio": junta
las noticias de prensa de cada zona afectada y las ancla FINO — si la noticia
nombra un sector geocodificado ("sector Las Rojas"), cuelga de ese punto; si
no, de su comuna. Nunca del paraguas regional (eso sería ruido, no memoria).

Fuente: Google News RSS (gratis, sin llave). Solo titular + medio + link:
agregamos y enlazamos, jamás copiamos contenido.

Acumulación: noticias.jsonl es PERMANENTE (append-only) — el archivo que
algún día responderá "¿qué ha pasado antes en esta zona?" a quien quiera
construir ahí. noticias.json es la vista compilada que lee el mapa.

Modales: caché con TTL (la mayoría de las corridas no consulta nada),
tope de consultas por corrida, pausa entre peticiones.
"""

import datetime as dt
import hashlib
import json
import pathlib
import re
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from urllib.parse import quote_plus

import requests

DIR = pathlib.Path(__file__).parent
ALERTS = DIR / "alerts.json"
SECTORES = DIR / "sectores.json"
GEOCACHE = DIR / "geocache.json"
ACUMULADO = DIR / "noticias.jsonl"      # memoria permanente, append-only
ESTADO = DIR / "noticias_state.json"    # caché de consultas (TTL)
SALIDA = DIR / "noticias.json"          # vista compilada para el mapa

VENTANA_ZONAS_DIAS = 45     # zonas con alertas en esta ventana reciben búsqueda
TTL_HORAS = 6               # una misma consulta no se repite antes de esto
MAX_CONSULTAS = 25          # tope duro por corrida
MAX_POR_ZONA = 10           # noticias mostradas por zona en el mapa
UA = {"User-Agent": "GeoInformesChile/1.0 (recopilador de titulares con enlace al medio; geoinformeschile@gmail.com)"}


def slug(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().split())


def ahora():
    return dt.datetime.now(dt.timezone.utc)


def rss_google_news(consulta):
    """Titulares de Google News para una consulta. Devuelve [(titulo, link, medio, fecha_iso)]."""
    url = ("https://news.google.com/rss/search?q=" + quote_plus(consulta)
           + "&hl=es-419&gl=CL&ceid=CL:es-419")
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    raiz = ET.fromstring(r.content)
    items = []
    for it in raiz.iter("item"):
        titulo = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        medio = (it.findtext("source") or "").strip()
        fecha = (it.findtext("pubDate") or "").strip()
        try:  # "Mon, 21 Jul 2026 14:30:00 GMT" -> ISO
            fecha = dt.datetime.strptime(fecha, "%a, %d %b %Y %H:%M:%S %Z")\
                      .replace(tzinfo=dt.timezone.utc).isoformat(timespec="seconds")
        except Exception:
            fecha = ""
        if titulo and link:
            items.append((titulo, link, medio, fecha))
    return items


def zonas_a_buscar():
    """Comunas con alguna alerta reciente (cualquier estado: la memoria no
    distingue vigente de cancelada) + sectores geocodificados con su punto."""
    comunas, contexto = {}, {}
    if ALERTS.exists():
        corte = ahora() - dt.timedelta(days=VENTANA_ZONAS_DIAS)
        for a in json.loads(ALERTS.read_text(encoding="utf-8")).get("alertas", []):
            try:
                t = dt.datetime.fromisoformat(str(a["ts"]).replace("Z", "+00:00"))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=dt.timezone.utc)
            except Exception:
                continue
            if t < corte:
                continue
            for c in a.get("comunas", []):
                comunas[slug(c)] = c
                contexto.setdefault(slug(c), a.get("tipo", ""))
    sectores = []
    if SECTORES.exists():
        for s in json.loads(SECTORES.read_text(encoding="utf-8")).get("sectores", []):
            sectores.append(s)  # trae nombre, lat, lon, lugar
    return comunas, contexto, sectores


def main():
    comunas, contexto, sectores = zonas_a_buscar()
    if not comunas and not sectores:
        print("Sin zonas recientes que buscar; nada que hacer.")
        SALIDA.write_text(json.dumps({"generado": ahora().isoformat(timespec="seconds"),
                                      "zonas": {}}, ensure_ascii=False), encoding="utf-8")
        return 0

    estado = {}
    if ESTADO.exists():
        try:
            estado = json.loads(ESTADO.read_text(encoding="utf-8"))
        except Exception:
            pass
    vistos_url = set()
    if ACUMULADO.exists():
        for linea in ACUMULADO.read_text(encoding="utf-8").splitlines():
            try:
                vistos_url.add(json.loads(linea)["id"])
            except Exception:
                pass

    # Índice de sectores por comuna para el anclaje fino
    sec_por_lugar = {}
    for s in sectores:
        sec_por_lugar.setdefault(slug(s.get("lugar", "")), []).append(s)

    consultas, nuevas_noticias = 0, 0
    corte_ttl = (ahora() - dt.timedelta(hours=TTL_HORAS)).isoformat()

    def buscar_y_guardar(consulta, comuna_nombre, comuna_slug):
        nonlocal consultas, nuevas_noticias
        k = slug(consulta)
        if estado.get(k, "") > corte_ttl or consultas >= MAX_CONSULTAS:
            return
        try:
            items = rss_google_news(consulta)
            consultas += 1
            time.sleep(0.6)
        except Exception as e:
            print(f"[news] ERROR '{consulta}': {e}", file=sys.stderr)
            return
        estado[k] = ahora().isoformat()
        with ACUMULADO.open("a", encoding="utf-8") as f:
            for titulo, link, medio, fecha in items:
                nid = hashlib.sha1(link.encode()).hexdigest()[:12]
                if nid in vistos_url:
                    continue
                vistos_url.add(nid)
                # Anclaje fino: ¿el titular nombra un sector geocodificado?
                anclaje = None
                for s in sec_por_lugar.get(comuna_slug, []):
                    if slug(s["nombre"]) in slug(titulo):
                        anclaje = {"nombre": s["nombre"], "lat": s["lat"], "lon": s["lon"]}
                        break
                f.write(json.dumps({
                    "id": nid, "comuna": comuna_nombre, "comuna_slug": comuna_slug,
                    "titulo": titulo, "medio": medio, "fecha": fecha, "url": link,
                    "sector": anclaje,
                    "registrado": ahora().isoformat(timespec="seconds"),
                }, ensure_ascii=False) + "\n")
                nuevas_noticias += 1

    # 1) Una consulta por comuna afectada (comuna + tipo de evento del boletín)
    for cslug, cnombre in comunas.items():
        tipo = contexto.get(cslug, "")
        buscar_y_guardar(f'"{cnombre}" {tipo}'.strip(), cnombre, cslug)
    # 2) Una por sector geocodificado (el anclaje más fino posible)
    for s in sectores:
        lug = s.get("lugar", "")
        buscar_y_guardar(f'"{s["nombre"]}" {lug}', lug, slug(lug))

    ESTADO.write_text(json.dumps(estado, ensure_ascii=False), encoding="utf-8")

    # ---- Compilar la vista para el mapa (desde TODA la memoria acumulada) ----
    zonas = {}
    if ACUMULADO.exists():
        for linea in ACUMULADO.read_text(encoding="utf-8").splitlines():
            try:
                n = json.loads(linea)
            except Exception:
                continue
            z = zonas.setdefault(n["comuna_slug"], {"comuna": n["comuna"], "noticias": []})
            z["noticias"].append({k: n.get(k) for k in
                                  ("titulo", "medio", "fecha", "url", "sector")})
    for z in zonas.values():
        z["noticias"].sort(key=lambda x: x.get("fecha") or "", reverse=True)
        z["noticias"] = z["noticias"][:MAX_POR_ZONA]

    SALIDA.write_text(json.dumps({
        "generado": ahora().isoformat(timespec="seconds"),
        "nota": ("Titulares de prensa con enlace al medio original. Recopilación "
                 "automática, no verificada en terreno."),
        "zonas": zonas,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"OK noticias | zonas={len(zonas)} consultas={consultas}/{MAX_CONSULTAS} "
          f"nuevas={nuevas_noticias} memoria={len(vistos_url)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
