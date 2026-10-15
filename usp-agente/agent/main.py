# agent/main.py — Servidor del agente de WhatsApp de USP (HUNO®)
"""
Basado en AgentKit (github.com/Hainrixz/whatsapp-agentkit, licencia MIT), adaptado para
cobranzas: lee comprobantes, los cruza con la cartola BCI y confirma pagos.

Rutas:
  GET  /           salud del servicio
  POST /webhook    eventos de Zernio (message.received y message.sent)
  GET  /admin      panel de USP (cartola, casos, clientes y teléfonos)
"""

import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

load_dotenv()

from agent import admin, config, escalamientos, flujo, memoria, tareas  # noqa: E402
from agent.db import inicializar_db  # noqa: E402
from agent.providers import obtener_proveedor  # noqa: E402
from agent.providers.base import MensajeEntrante  # noqa: E402

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("usp")
logger.setLevel(logging.DEBUG if ENVIRONMENT == "development" else logging.INFO)

_candados: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
# Mensajes de texto seguidos ("hola" / "una consulta" / "cuanto debo") se juntan y se responden
# una sola vez: menos ruido para el cliente y menos tokens.
AGRUPAR_SEGUNDOS = float(os.getenv("AGRUPAR_SEGUNDOS", "3"))
_en_espera: dict[str, list[MensajeEntrante]] = defaultdict(list)
# Un mensaje saliente que no paso por Zernio (sentVia null) a pocos segundos de uno entrante es
# la bienvenida / ausencia automatica de WhatsApp Business: NO es un ejecutivo, no pausa.
SEGUNDOS_RESPUESTA_AUTOMATICA = float(os.getenv("SEGUNDOS_RESPUESTA_AUTOMATICA", "15"))
ESPERA_CLASIFICAR_SALIENTE = float(os.getenv("ESPERA_CLASIFICAR_SALIENTE", "5"))
REINTENTOS_ENVIO = (1, 3)  # segundos entre reintentos si Zernio falla al enviar

proveedor = None
error_configuracion: str | None = None
try:
    proveedor = obtener_proveedor()
except Exception as e:  # noqa: BLE001
    error_configuracion = str(e)
estado_proveedor = {"ok": None, "detalle": "sin verificar"}
# Si la base falla al arrancar NO se reinicia el contenedor en bucle: con una clave mala, cada
# reinicio es un intento fallido y Supabase bloquea la conexion (ECIRCUITBREAKER). Se reintenta
# con calma cada REINTENTO_DB segundos y mientras tanto el servicio responde "error" en /.
estado_db = {"ok": False, "detalle": "sin conectar"}
REINTENTO_DB = 300


async def conectar_db() -> bool:
    try:
        await inicializar_db()
        await memoria.limpiar_eventos_viejos()
    except Exception as e:  # noqa: BLE001
        texto = str(e).splitlines()[0][:300]
        if "password authentication failed" in texto or "ECIRCUITBREAKER" in texto:
            texto += " -> revisa la contrasena en DATABASE_URL (Railway -> Variables)"
        estado_db.update(ok=False, detalle=texto)
        logger.error(f"No se pudo conectar a la base de datos: {texto}")
        return False
    estado_db.update(ok=True, detalle="conectada")
    logger.info("Base de datos conectada")
    return True


async def reintentar_db():
    while not await conectar_db():
        await asyncio.sleep(REINTENTO_DB)
    _iniciar_revision()


_tarea_revision: asyncio.Task | None = None


def _iniciar_revision():
    global _tarea_revision
    if proveedor is not None and (_tarea_revision is None or _tarea_revision.done()) \
            and os.getenv("REVISION_PERIODICA", "1") != "0":
        _tarea_revision = asyncio.create_task(tareas.ciclo(proveedor))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global estado_proveedor
    tarea_db = None
    if not await conectar_db():
        tarea_db = asyncio.create_task(asyncio.sleep(REINTENTO_DB))
        tarea_db.add_done_callback(lambda _: asyncio.ensure_future(reintentar_db()))
    else:
        _iniciar_revision()
    app.state.proveedor = proveedor
    if proveedor:
        ok, detalle = await proveedor.verificar_conexion()
        estado_proveedor = {"ok": ok, "detalle": detalle}
        logger.info(f"Zernio: {'OK' if ok else 'ERROR'} — {detalle}")
    else:
        logger.error(f"Proveedor no configurado: {error_configuracion}")
    yield
    if tarea_db:
        tarea_db.cancel()
    if _tarea_revision:
        _tarea_revision.cancel()


app = FastAPI(title="Agente USP — HUNO®", version="1.0.0", lifespan=lifespan,
              docs_url=None if ENVIRONMENT == "production" else "/docs", redoc_url=None)
app.include_router(admin.router)


@app.get("/")
async def salud():
    if error_configuracion:
        return {"status": "error", "detalle": error_configuracion}
    if not estado_db["ok"]:
        return {"status": "error", "base_de_datos": estado_db["detalle"]}
    return {"status": "ok" if estado_proveedor["ok"] else "degradado", "servicio": "agente-usp",
            "conexion": estado_proveedor}


@app.post("/webhook")
async def webhook(request: Request, tareas_fondo: BackgroundTasks):
    """Responde 200 de inmediato; Zernio reintenta si no recibe 2xx en 5 segundos."""
    if proveedor is None:
        raise HTTPException(503, error_configuracion or "Proveedor no configurado")
    if not estado_db["ok"]:
        raise HTTPException(503, "Base de datos no disponible")  # Zernio reintenta mas tarde
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
        if msg.tipo in ("saliente_humano", "saliente_desconocido"):
            if await memoria.marcar_evento(evento_id):
                tareas_fondo.add_task(registrar_saliente, msg)
            continue
        if not msg.telefono or (not msg.texto.strip() and not msg.adjuntos):
            continue  # reacciones, ubicaciones, etc.: no necesitan respuesta
        if not await memoria.marcar_evento(evento_id):
            logger.info(f"Evento repetido: {evento_id}")
            continue
        # Se anota la hora de llegada al tiro (sirve para reconocer respuestas automaticas)
        try:
            await memoria.registrar_entrante(msg.telefono, msg.contexto, msg.nombre)
        except Exception:  # noqa: BLE001 — el evento ya quedo marcado: no perder el mensaje
            logger.exception("No se pudo registrar la llegada del mensaje")
        tareas_fondo.add_task(procesar_mensaje, msg)
        encolados += 1
    return {"status": "ok", "encolados": encolados}


async def registrar_saliente(msg: MensajeEntrante):
    """Un mensaje que salio del numero sin pasar por este agente."""
    conv_id = msg.contexto.get("conversation_id")
    if msg.tipo == "saliente_desconocido":
        # Puede ser un ejecutivo en la app del celular o la respuesta automatica de WhatsApp Business:
        # se espera un poco por si el mensaje del cliente que la provoco llega despues.
        await asyncio.sleep(ESPERA_CLASIFICAR_SALIENTE)
        segundos = await memoria.segundos_desde_entrante(msg.telefono, conv_id)
        if segundos is not None and segundos <= SEGUNDOS_RESPUESTA_AUTOMATICA + ESPERA_CLASIFICAR_SALIENTE:
            logger.info(f"{msg.telefono}: respuesta automatica de WhatsApp Business, no pausa al agente")
            return
    horas = int(config.reglas()["pausa_humano_horas"])
    if await memoria.pausar_por_humano(conv_id, msg.telefono, horas):
        logger.info(f"Un ejecutivo respondio en {conv_id}: agente en pausa {horas} h")
    if msg.texto.strip() and msg.telefono:
        # Queda en el historial para que, al volver, el agente sepa lo que se converso
        await memoria.guardar_mensaje(msg.telefono, "assistant", f"[Respondió un ejecutivo de USP]: {msg.texto.strip()}")


def _parece_incompleto(texto: str) -> bool:
    from agent.cerebro import _AFIRMACIONES, _GRACIAS, _SALUDOS, _limpio

    t = _limpio(texto or "")
    if t in _SALUDOS:
        return True
    if t in _GRACIAS or t in _AFIRMACIONES or "?" in texto or "¿" in texto:
        return False
    return len(t.split()) <= 3


async def _agrupar(msg: MensajeEntrante) -> MensajeEntrante | None:
    """
    Espera unos segundos por si el cliente sigue escribiendo. Devuelve el mensaje combinado
    o None si un mensaje posterior se encarga de responder.
    """
    if msg.adjuntos or AGRUPAR_SEGUNDOS <= 0:
        return msg
    cola = _en_espera[msg.telefono]
    cola.append(msg)
    # Solo se espera si el mensaje parece la primera parte de algo ("hola", "una consulta"). Una
    # pregunta completa se responde al tiro (y se lleva consigo el "hola" que estaba esperando).
    if _parece_incompleto(msg.texto):
        await asyncio.sleep(AGRUPAR_SEGUNDOS)
    if not cola or cola[-1] is not msg:
        return None
    grupo = list(cola)
    cola.clear()
    if len(grupo) == 1:
        return msg
    return MensajeEntrante(telefono=msg.telefono, texto="\n".join(m.texto.strip() for m in grupo if m.texto.strip()),
                           mensaje_id=msg.mensaje_id, tipo=msg.tipo, nombre=msg.nombre, adjuntos=[],
                           contexto=msg.contexto)


async def _enviar(msg: MensajeEntrante, texto: str, clave: str) -> bool:
    ctx = dict(msg.contexto, idempotencia=clave)
    for espera in (0, *REINTENTOS_ENVIO):
        if espera:
            await asyncio.sleep(espera)
        if await proveedor.enviar_mensaje(msg.telefono, texto, ctx):
            return True
    logger.error(f"No se pudo responder a {msg.telefono}")
    return False


async def procesar_mensaje(msg: MensajeEntrante):
    msg = await _agrupar(msg)
    if msg is None:
        return
    inicio = time.monotonic()
    evento_id = msg.contexto.get("evento_id") or msg.mensaje_id
    async with _candados[msg.telefono]:
        try:
            respuestas, turno = await flujo.procesar(proveedor, msg)
        except Exception:  # noqa: BLE001
            # Nunca dejar al cliente sin respuesta: se le avisa y el caso queda para una persona
            logger.exception(f"Error procesando mensaje de {msg.telefono}")
            try:
                await escalamientos.crear(msg.telefono, "error_tecnico",
                                          f"Falla técnica procesando: {(msg.texto or '[archivo]')[:500]}")
            except Exception:  # noqa: BLE001
                logger.exception("Tampoco se pudo crear el caso")
            await _enviar(msg, config.respuesta("error"), f"{evento_id}-error")
            return
        enviados = []
        for i, texto in enumerate(respuestas):
            if not (texto or "").strip():
                continue
            if not await _enviar(msg, texto, f"{evento_id}-{i}"):
                # WhatsApp/Zernio no acepto el envio: que lo vea una persona
                await escalamientos.crear(msg.telefono, "error_tecnico",
                                          "No se pudo enviar la respuesta por WhatsApp. Respuesta pendiente: " + texto[:800])
                break
            enviados.append(texto)
        if turno and enviados:
            await memoria.guardar_turno(msg.telefono, turno, "\n\n".join(enviados))
        logger.info(f"{msg.telefono}: respondido en {time.monotonic() - inicio:.1f} s")
