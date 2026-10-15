# agent/flujo.py — Que hacer con cada mensaje que entra
"""
Todo mensaje recibe respuesta, salvo que la conversacion la este atendiendo un ejecutivo
(en ese caso solo se avisan los pagos confirmados).

- Imagen o PDF: se lee como comprobante y se concilia. Si no es comprobante, el archivo
  queda guardado y se deriva a un ejecutivo (puede ser un documento para otra area).
- Otros formatos (Word, Excel...): se guardan y se derivan.
- Audio, video, sticker: respuesta fija.
- Texto: reglas fijas sin tokens y, si no calza ninguna, la IA con herramientas.
- Texto que acompana un archivo: si trae una pregunta o algo mas que "aqui va el pago",
  tambien se responde; si trae un RUT, se usa para asociar el comprobante.
"""

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select

from agent import cerebro, comprobantes, config, conciliacion, escalamientos, memoria
from agent.db import Comprobante, ahora, async_session
from agent.providers.base import Adjunto, MensajeEntrante, ProveedorWhatsApp
from agent.utils import buscar_ruts, formatear_monto

logger = logging.getLogger("usp")

TIPOS_ARCHIVO = {"image", "file", "document"}
_EXTENSIONES = {
    "application/msword": "Word", "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "Word",
    "application/vnd.ms-excel": "Excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "Excel",
    "text/csv": "CSV", "application/zip": "ZIP", "text/plain": "texto", "image/heic": "imagen HEIC",
}
_RE_PIDE_RESPUESTA = re.compile(r"[?¿]|\b(ejecutiv|persona|humano|reclam|urgente|ayuda|ayudar|necesito|quiero|consulta)", re.I)


@dataclass
class ResultadoAdjunto:
    texto: str          # respuesta al cliente
    estado: str         # estado del comprobante (o tipo de adjunto)
    nota: str           # como queda en el historial de la conversacion


def _texto_cliente(msg: MensajeEntrante) -> str:
    t = (msg.texto or "").strip()
    return f"\nMensaje del cliente: {t[:500]}" if t else ""


async def _guardar_comprobante(msg: MensajeEntrante, contenido: bytes | None, mime: str | None,
                               estado: str = "recibido", motivo: str | None = None) -> int:
    async with async_session() as s:
        c = Comprobante(
            telefono=msg.telefono,
            conversation_id=msg.contexto.get("conversation_id"),
            account_id=msg.contexto.get("account_id"),
            mensaje_id=msg.mensaje_id,
            archivo=contenido,
            mime=(mime or "")[:100] or None,
            sha256=hashlib.sha256(contenido).hexdigest() if contenido else None,
            estado=estado,
            motivo=motivo,
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


async def _ilegibles_recientes(telefono: str, sin_contar: int) -> int:
    """Comprobantes ilegibles del mismo numero en las ultimas 24 h (para no pedir reenvio en bucle)."""
    async with async_session() as s:
        return (await s.execute(
            select(func.count()).select_from(Comprobante).where(
                Comprobante.telefono == telefono, Comprobante.id != sin_contar,
                Comprobante.estado == "no_legible", Comprobante.motivo == "faltan_monto_o_fecha",
                Comprobante.creado_en >= ahora() - timedelta(hours=24))
        )).scalar_one()


def _nombre_formato(mime: str) -> str:
    return _EXTENSIONES.get(mime, mime.split("/")[-1][:20] if mime else "desconocido")


async def procesar_adjunto(proveedor: ProveedorWhatsApp, msg: MensajeEntrante, adjunto: Adjunto) -> ResultadoAdjunto:
    tel = msg.telefono
    if adjunto.tipo == "audio":
        return ResultadoAdjunto(config.respuesta("audio"), "audio", "[Envió un audio]")
    if adjunto.tipo == "video":
        return ResultadoAdjunto(config.respuesta("video"), "video", "[Envió un video]")
    if adjunto.tipo not in TIPOS_ARCHIVO:
        return ResultadoAdjunto(config.respuesta("adjunto_no_soportado"), adjunto.tipo,
                                f"[Envió un adjunto de tipo {adjunto.tipo}]")

    descarga = await proveedor.descargar_adjunto(adjunto)
    if descarga is None:
        cid = await _guardar_comprobante(msg, None, adjunto.mime, "escalado", "error_lectura")
        await escalamientos.crear(tel, "error_lectura", "No se pudo descargar el archivo desde WhatsApp. Pedir que lo reenvíe."
                                  + _texto_cliente(msg), cid)
        return ResultadoAdjunto(config.respuesta("recibido_revision"), "escalado", "[Envió un archivo que no se pudo descargar]")
    contenido, mime = descarga
    mime = comprobantes.detectar_mime(contenido, mime)

    if not comprobantes.soportado(mime):
        # Word, Excel, etc.: puede ser un documento para contabilidad o RRHH. Se guarda y lo ve una persona.
        formato = _nombre_formato(mime)
        cid = await _guardar_comprobante(msg, contenido, mime, "no_legible", "formato_no_soportado")
        await escalamientos.crear(tel, "documento_recibido",
                                  f"Archivo {formato} ({max(1, len(contenido) // 1024)} KB) que el agente no puede leer."
                                  + _texto_cliente(msg), cid)
        return ResultadoAdjunto(config.respuesta("documento_no_soportado"), "documento", f"[Envió un archivo {formato}]")

    cid = await _guardar_comprobante(msg, contenido, mime)
    try:
        datos = await comprobantes.leer(contenido, mime)
    except Exception:  # noqa: BLE001
        logger.exception("Error leyendo el archivo")
        datos = None
    if datos is None:
        async with async_session() as s:
            c = await s.get(Comprobante, cid)
            c.estado, c.motivo = "escalado", "error_lectura"
            await s.commit()
        await escalamientos.crear(tel, "error_lectura", "La IA no pudo leer el archivo." + _texto_cliente(msg), cid)
        return ResultadoAdjunto(config.respuesta("recibido_revision"), "escalado", "[Envió un archivo que no se pudo leer]")
    await _actualizar_lectura(cid, datos)
    resultado = await conciliacion.evaluar(cid)
    texto = resultado.mensaje()
    tipo = "PDF" if mime == comprobantes.MIME_PDF else "imagen"

    if resultado.estado == "no_es_comprobante":
        que = datos.tipo_documento or "no es un comprobante"
        await escalamientos.crear(tel, "documento_recibido", f"{tipo.capitalize()} recibida: {que}." + _texto_cliente(msg), cid)
        return ResultadoAdjunto(texto, "documento", f"[Envió un archivo ({tipo}) que no es comprobante: {que}]")
    if resultado.estado == "no_legible" and await _ilegibles_recientes(tel, cid) >= 1:
        # Segundo comprobante ilegible seguido: en vez de pedirlo otra vez, lo ve una persona
        resultado = await conciliacion.escalar(cid, "error_lectura", "Varios comprobantes ilegibles seguidos." + _texto_cliente(msg))
        texto = config.respuesta("recibido_revision")
    elif resultado.estado == "escalado" and msg.texto.strip():
        # Lo que escribio el cliente junto al archivo ayuda al ejecutivo ("pago facturas 1501 y 1502")
        async with async_session() as s:
            propio = (await s.get(Comprobante, cid)).estado == "escalado"
        if propio:
            await escalamientos.crear(tel, resultado.motivo, _texto_cliente(msg).strip(), cid)

    if resultado.estado == "confirmado":
        async with async_session() as s:
            c = await s.get(Comprobante, cid)
            c.notificado = True  # se avisa en esta misma respuesta
            await s.commit()
    monto = formatear_monto(datos.monto) if datos.monto else "monto no leído"
    return ResultadoAdjunto(texto, resultado.estado, f"[Envió un comprobante ({tipo}) por {monto}: {resultado.estado}]")


def _merece_respuesta(texto: str) -> bool:
    """El texto que acompana un archivo se responde si trae una pregunta o un pedido."""
    return bool(_RE_PIDE_RESPUESTA.search(texto)) or len(texto.split()) >= 12


async def procesar(proveedor: ProveedorWhatsApp, msg: MensajeEntrante) -> tuple[list[str], str | None]:
    """
    Devuelve (respuestas a enviar, texto a guardar como turno del usuario).
    Lista vacia = no responder (conversacion tomada por un ejecutivo).
    """
    await memoria.registrar_entrante(msg.telefono, msg.contexto, msg.nombre)
    texto = (msg.texto or "").strip()
    adjuntos = [a for a in msg.adjuntos if a.tipo != "sticker"]
    if not texto and not adjuntos and msg.adjuntos:
        texto = "👍"  # un sticker suelto suele ser un "gracias" / "ok"

    if await memoria.esta_pausada(msg.telefono):
        logger.info(f"{msg.telefono}: conversacion atendida por un ejecutivo, el agente no responde")
        # Los comprobantes se procesan igual (el ejecutivo los ve validados) y un pago
        # confirmado se avisa: es informacion que el cliente necesita y no choca con el ejecutivo.
        avisos, notas = [], []
        for a in adjuntos:
            try:
                r = await procesar_adjunto(proveedor, msg, a)
                notas.append(r.nota)
                if r.estado == "confirmado":
                    avisos.append(r.texto)
            except Exception:  # noqa: BLE001
                logger.exception("Error procesando adjunto en conversacion pausada")
        if texto:
            notas.append(texto)
        if avisos:
            return avisos, "\n".join(notas)
        await memoria.guardar_mensaje(msg.telefono, "user", "\n".join(notas))
        return [], None

    # Un RUT escrito junto al comprobante sirve para asociarlo (nunca para ver deudas)
    if adjuntos and texto:
        ruts = [r for r in buscar_ruts(texto) if r != config.rut_propio()]
        if ruts:
            await memoria.guardar_rut_declarado(msg.telefono, ruts[0])

    respuestas: list[str] = []
    turno: list[str] = []
    for a in adjuntos:
        r = await procesar_adjunto(proveedor, msg, a)
        respuestas.append(r.texto)
        turno.append(r.nota)

    if texto:
        if adjuntos and not _merece_respuesta(texto):
            turno.append(texto)
        else:
            previo = ("[Junto con este mensaje envió un archivo y el sistema ya le respondió: «"
                      + " / ".join(respuestas) + "». No repitas esa respuesta.]") if respuestas else ""
            salida, guardar = await cerebro.responder(msg.telefono, texto, previo=previo)
            if salida and salida not in respuestas:
                respuestas.append(salida)
            if guardar:
                turno.append(texto)

    return respuestas, "\n".join(turno) if turno else None
