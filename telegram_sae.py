#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GeoInformesChile — listener del canal público de Telegram de SENAPRED. (v2)

Cambios v2 (tras probar contra el historial real del canal):
  - Primera carga: SOLO los últimos DIAS_PRIMERA_CARGA días (antes partía
    desde el mensaje 1 del canal, año 2023, y ensuciaba el mapa con pasado).
  - El nombre de comuna corta antes de "Región ..." ("Linares Región de Maule").
  - SAE se marca solo si el mensaje CONFIRMA activación ("activó", "solicita
    evacuar"); los avisos educativos ("activará...") ya no cuentan como alerta.

Modo poll apto para cron: cada corrida baja solo lo nuevo desde el último id.
Requiere TG_API_ID / TG_API_HASH en .env (my.telegram.org). La primera
ejecución es interactiva y crea geoinformes.session — esa llave no se comparte.
"""

import datetime as dt
import json
import os
import pathlib
import re
import sys

DIR = pathlib.Path(__file__).parent
STATE = DIR / "state_telegram.json"
OUT = DIR / "telegram_alertas.jsonl"
CANAL = "SenapredChile"
MAX_POR_CORRIDA = 200
DIAS_PRIMERA_CARGA = 7   # en la primera corrida, no mirar más atrás que esto


def cargar_env():
    p = DIR / ".env"
    if p.exists():
        for linea in p.read_text(encoding="utf-8").splitlines():
            linea = linea.strip()
            if linea and not linea.startswith("#") and "=" in linea:
                k, _, v = linea.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


# ------------------------- parser de boletines --------------------------------

RE_NIVEL = [
    (re.compile(r"alerta\s+roja", re.I), "ROJA"),
    (re.compile(r"alerta\s+amarilla", re.I), "AMARILLA"),
    (re.compile(r"temprana\s+preventiva", re.I), "ATP"),
]
RE_CANCELA = re.compile(r"se\s+cancela|cancelaci[oó]n\s+de", re.I)
RE_SAE = re.compile(r"\bSAE\b|mensajer[ií]a\s+SAE", re.I)
# SAE real = activación confirmada, no un "activará" educativo a futuro.
RE_SAE_CONFIRMA = re.compile(r"activ[oó]\b|solicita\s+evacuar", re.I)
# El nombre de comuna termina antes de puntuación, de " por <causa>" o de "Región".
RE_COMUNA = re.compile(
    r"comunas?\s+de\s+([^,.;\n#]+?)(?=\s+por\s|\s+[Rr]egi[oó]n\b|\s*[,.;\n#]|$)")
RE_REGION = re.compile(
    r"regi[oó]n\s+(?:de\s+la\s+|del\s+|de\s+)?([^,.;\n#]+?)(?=\s+por\s|\s*[,.;\n#]|$)", re.I)

TIPOS = {
    "aluvi": "aluvión", "crecid": "crecida de río", "desbord": "desborde",
    "remoci": "remoción en masa", "quebrad": "activación de quebradas",
    "marejad": "marejadas", "tormenta": "tormentas eléctricas", "viento": "viento",
    "nevad": "nevadas", "meteorol": "evento meteorológico",
    "incendio": "incendio forestal", "volc": "actividad volcánica",
    "tsunami": "tsunami", "evacu": "evacuación",
}


def parsear(texto, msg_id, fecha):
    if not texto:
        return None
    nivel = next((n for rx, n in RE_NIVEL if rx.search(texto)), None)
    sae = bool(RE_SAE.search(texto)) and bool(RE_SAE_CONFIRMA.search(texto))
    if not nivel and not sae:
        return None  # mensaje institucional/educativo, no es alerta

    t = texto.lower()
    tipo = next((v for k, v in TIPOS.items() if k in t), "otro")
    m_com = RE_COMUNA.search(texto)
    m_reg = RE_REGION.search(texto)
    return {
        "tg_id": msg_id,
        "fecha": fecha,
        "nivel": nivel or ("ROJA" if sae else None),  # SAE confirmado sin nivel = grave
        "sae": sae,
        "estado": "CANCELADA" if RE_CANCELA.search(texto) else "VIGENTE",
        "tipo_alerta": tipo,
        "comuna": m_com.group(1).strip() if m_com else "",
        "region": m_reg.group(1).strip() if m_reg else "",
        "titulo": texto.strip().splitlines()[0][:160],
        "descripcion": texto.strip()[:600],
        "url": f"https://t.me/{CANAL}/{msg_id}",
    }


# ------------------------------- main -----------------------------------------

def main():
    cargar_env()
    api_id, api_hash = os.environ.get("TG_API_ID"), os.environ.get("TG_API_HASH")
    if not api_id or not api_hash:
        print("Faltan TG_API_ID / TG_API_HASH (my.telegram.org).", file=sys.stderr)
        return 1

    from telethon.sync import TelegramClient  # import tardío: falla claro si no está

    ultimo = 0
    if STATE.exists():
        ultimo = json.loads(STATE.read_text()).get("ultimo_id", 0)

    corte = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=DIAS_PRIMERA_CARGA)
    nuevos = 0
    with TelegramClient(str(DIR / "geoinformes"), int(api_id), api_hash) as client:
        with OUT.open("a", encoding="utf-8") as f:

            if ultimo == 0:
                # Primera carga: del más nuevo hacia atrás, frenando en el corte.
                mensajes = []
                for msg in client.iter_messages(CANAL, limit=MAX_POR_CORRIDA):
                    if msg.date.astimezone(dt.timezone.utc) < corte:
                        break
                    mensajes.append(msg)
                mensajes.reverse()  # dejarlos en orden cronológico
            else:
                # Corridas siguientes: solo lo nuevo desde el último id.
                mensajes = list(client.iter_messages(
                    CANAL, min_id=ultimo, reverse=True, limit=MAX_POR_CORRIDA))

            for msg in mensajes:
                ultimo = max(ultimo, msg.id)
                reg = parsear(msg.message or "", msg.id,
                              msg.date.astimezone(dt.timezone.utc).isoformat(timespec="seconds"))
                if reg:
                    f.write(json.dumps(reg, ensure_ascii=False) + "\n")
                    nuevos += 1

    STATE.write_text(json.dumps({"ultimo_id": ultimo,
                                 "corrida": dt.datetime.now(dt.timezone.utc).isoformat()}))
    print(f"OK telegram | nuevos={nuevos} ultimo_id={ultimo}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
