# agent/main.py — Servidor del agente de WhatsApp de USP (HUNO®)
"""
Basado en AgentKit (github.com/Hainrixz/whatsapp-agentkit, licencia MIT), adaptado para
cobranzas: lee comprobantes, los cruza con la cartola BCI y confirma pagos.

Rutas:
  GET  /           salud del servicio
  POST /webhook    eventos de Zernio (message.received y message.sent)
  GET  /admin      panel de USP (cartola, facturas, contactos, casos)
"""

import asyncio
import json
import logging
import os
from collections import defaultdict
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

load_dotenv()

from agent import admin, config, flujo, memoria  # noqa: E402
from agent.db import inicializar_db  # noqa: E402
from agent.providers import obtener_proveedor  # noqa: E402
from agent.providers.base import MensajeEntrante  # noqa: E402

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("usp")
logger.setLevel(logging.DEBUG if ENVIRONMENT == "development" else logging.INFO)

_candados: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

proveedor = None
error_configuracion: str | None = None
try:
    proveedor = obtener_proveedor()
except Exception as e:  # noqa: BLE001
    error_configuracion = str(e)
estado_proveedor = {"ok": None, "detalle": "sin verificar"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global estado_proveedor
    await inicializar_db()
    await memoria.limpiar_eventos_viejos()
    app.state.proveedor = proveedor
    if proveedor:
        ok, detalle = await proveedor.verificar_conexion()
        estado_proveedor = {"ok": ok, "detalle": detalle}
        logger.info(f"Zernio: {'OK' if ok else 'ERROR'} — {detalle}")
    else:
        logger.error(f"Proveedor no configurado: {error_configuracion}")
    yield


app = FastAPI(title="Agente USP — HUNO®", version="1.0.0", lifespan=lifespan,
              docs_url=None if ENVIRONMENT == "production" else "/docs", redoc_url=None)
app.include_router(admin.router)


@app.get("/")
async def salud():
    if error_configuracion:
        return {"status": "error", "detalle": error_configuracion}
    return {"status": "ok" if estado_proveedor["ok"] else "degradado", "servicio": "agente-usp",
            "conexion": estado_proveedor}


@app.post("/webhook")
async def webhook(request: Request, tareas: BackgroundTasks):
    """Responde 200 de inmediato; Zernio reintenta si no recibe 2xx en 5 segundos."""
    if proveedor is None:
        raise HTTPException(503, error_configuracion or "Proveedor no configurado")
    cuerpo = await request.body()
    if not await proveedor.verificar_firma(request, cuerpo):
        raise HTTPException(401, "Firma invalida")
    try:
        mensajes = await proveedor.parsear_webhook(json.loads(cuerpo))
    except Exception as e:  # noqa: BLE001
        logger.error(f"Webhook ilegible: {e}")
        return {"status": "ignorado"}

    encolados = 0
    for msg in mensajes:
        evento_id = msg.contexto.get("evento_id") or msg.mensaje_id
        if msg.tipo == "saliente_api":
            continue
        if msg.tipo == "saliente_humano":
            if await memoria.marcar_evento(evento_id):
                horas = int(config.reglas()["pausa_humano_horas"])
                if await memoria.pausar_por_humano(msg.contexto.get("conversation_id"), msg.telefono, horas):
                    logger.info(f"Un ejecutivo respondio en {msg.contexto.get('conversation_id')}: agente en pausa {horas} h")
            continue
        if not msg.telefono or (not msg.texto.strip() and not msg.adjuntos):
            continue
        if not await memoria.marcar_evento(evento_id):
            logger.info(f"Evento repetido: {evento_id}")
            continue
        tareas.add_task(procesar_mensaje, msg)
        encolados += 1
    return {"status": "ok", "encolados": encolados}


async def procesar_mensaje(msg: MensajeEntrante):
    evento_id = msg.contexto.get("evento_id") or msg.mensaje_id
    async with _candados[msg.telefono]:
        try:
            respuestas, turno = await flujo.procesar(proveedor, msg)
            for i, texto in enumerate(respuestas):
                ctx = dict(msg.contexto, idempotencia=f"{evento_id}-{i}")
                if not await proveedor.enviar_mensaje(msg.telefono, texto, ctx):
                    logger.error(f"No se pudo responder a {msg.telefono}")
                    if i == 0:
                        await memoria.liberar_evento(evento_id)  # que el reintento de Zernio se procese
                    return
            if turno and respuestas:
                await memoria.guardar_turno(msg.telefono, turno, "\n\n".join(respuestas))
        except Exception:  # noqa: BLE001
            logger.exception(f"Error procesando mensaje de {msg.telefono}")
            await memoria.liberar_evento(evento_id)
