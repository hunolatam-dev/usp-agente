# agent/flujo.py — Que hacer con cada mensaje que entra
import hashlib
import logging

from agent import cerebro, comprobantes, config, conciliacion, escalamientos, memoria
from agent.db import Comprobante, async_session
from agent.providers.base import Adjunto, MensajeEntrante, ProveedorWhatsApp

logger = logging.getLogger("usp")

TIPOS_ARCHIVO = {"image", "file", "document"}


async def _guardar_comprobante(msg: MensajeEntrante, contenido: bytes | None, mime: str | None) -> int:
    async with async_session() as s:
        c = Comprobante(
            telefono=msg.telefono,
            conversation_id=msg.contexto.get("conversation_id"),
            account_id=msg.contexto.get("account_id"),
            mensaje_id=msg.mensaje_id,
            archivo=contenido,
            mime=mime,
            sha256=hashlib.sha256(contenido).hexdigest() if contenido else None,
        )
        s.add(c)
        await s.commit()
        return c.id


async def _actualizar_lectura(cid: int, datos: comprobantes.DatosComprobante):
    async with async_session() as s:
        c = await s.get(Comprobante, cid)
        c.es_comprobante = datos.es_comprobante
        if datos.legible:
            c.monto, c.fecha = datos.monto, datos.fecha
        c.numero_operacion = datos.numero_operacion
        c.banco_origen = (datos.banco_origen or "")[:100] or None
        c.rut_origen = datos.rut_origen
        c.nombre_origen = (datos.nombre_origen or "")[:200] or None
        c.cuenta_destino = (datos.cuenta_destino or "")[:60] or None
        c.rut_destino = datos.rut_destino
        c.datos_crudos = datos.crudo
        await s.commit()


async def procesar_adjunto(proveedor: ProveedorWhatsApp, msg: MensajeEntrante, adjunto: Adjunto) -> str:
    if adjunto.tipo == "audio":
        return config.respuesta("audio")
    if adjunto.tipo not in TIPOS_ARCHIVO:
        return config.respuesta("adjunto_no_soportado")

    descarga = await proveedor.descargar_adjunto(adjunto)
    if descarga is None:
        cid = await _guardar_comprobante(msg, None, adjunto.mime)
        await escalamientos.crear(msg.telefono, "error_lectura", "No se pudo descargar el archivo desde WhatsApp", cid)
        return config.respuesta("recibido_revision")
    contenido, mime = descarga
    mime = comprobantes.detectar_mime(contenido, mime)
    if not comprobantes.soportado(mime):
        return config.respuesta("adjunto_no_soportado")

    cid = await _guardar_comprobante(msg, contenido, mime)
    datos = await comprobantes.leer(contenido, mime)
    if datos is None:
        await escalamientos.crear(msg.telefono, "error_lectura", "Claude no pudo leer el archivo", cid)
        return config.respuesta("recibido_revision")
    await _actualizar_lectura(cid, datos)
    resultado = await conciliacion.evaluar(cid)
    if resultado.estado == "confirmado":
        async with async_session() as s:
            c = await s.get(Comprobante, cid)
            c.notificado = True  # se avisa en esta misma respuesta
            await s.commit()
    return resultado.mensaje()


async def procesar(proveedor: ProveedorWhatsApp, msg: MensajeEntrante) -> tuple[list[str], str | None]:
    """
    Devuelve (respuestas a enviar, texto a guardar como turno del usuario).
    Lista vacia = no responder (conversacion tomada por un ejecutivo).
    """
    await memoria.registrar_entrante(msg.telefono, msg.contexto, msg.nombre)

    if await memoria.esta_pausada(msg.telefono):
        logger.info(f"{msg.telefono}: conversacion atendida por un ejecutivo, el agente no responde")
        # Aun asi se procesan los comprobantes, para que el ejecutivo los vea validados
        for a in msg.adjuntos:
            try:
                await procesar_adjunto(proveedor, msg, a)
            except Exception:  # noqa: BLE001
                logger.exception("Error procesando adjunto en conversacion pausada")
        return [], None

    respuestas: list[str] = []
    turno_usuario = []
    for a in msg.adjuntos:
        texto = await procesar_adjunto(proveedor, msg, a)
        respuestas.append(texto)
        turno_usuario.append(f"[Envio un archivo: {a.tipo}]")

    if msg.texto.strip():
        if msg.adjuntos:
            # El texto que acompaña un comprobante ("aqui va el pago") no necesita otra respuesta
            turno_usuario.append(msg.texto.strip())
        else:
            texto, guardar = await cerebro.responder(msg.telefono, msg.texto)
            respuestas.append(texto)
            if not guardar:
                return respuestas, None
            turno_usuario.append(msg.texto.strip())

    return respuestas, "\n".join(turno_usuario) if turno_usuario else None
