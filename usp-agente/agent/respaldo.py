# agent/respaldo.py — Conservacion de los archivos que llegan por WhatsApp
"""
Regla de HUNO: los archivos (comprobantes, documentos) se conservan 180 dias corridos desde
que llegan. Al dia 170 se envian por correo a alertas@huno.cl como respaldo y al dia 180 se
borra el archivo de la base (los datos leidos del comprobante se quedan).

- Nunca se borra un archivo que no se haya respaldado. Si el respaldo falla, se reintenta al
  dia siguiente y, si llega el dia 180 sin respaldo, se abre un caso para revisarlo.
- Railway (plan Hobby) bloquea el correo por SMTP, asi que el envio pasa por
  huno.cl/api/usp_respaldo.php (cPanel, como los demas correos de HUNO). Ese PHP solo envia a
  alertas@huno.cl y exige el token RESPALDO_TOKEN.
- Cada correo lleva hasta ~3 MB de archivos (cabe en el limite tipico de PHP); si hay mas, se
  envian varios correos.
"""

import base64
import logging
import os
from datetime import timedelta
from html import escape

import httpx
from sqlalchemy import func, select, update

from agent import config, escalamientos, horario, huno
from agent.db import Comprobante, ahora, async_session, como_utc
from agent.utils import formatear_monto

logger = logging.getLogger("usp")

RESPALDO_URL = os.getenv("RESPALDO_URL") or "https://huno.cl/api/usp_respaldo.php"
LOTE_BYTES = int(os.getenv("RESPALDO_LOTE_BYTES") or 3 * 1024 * 1024)
TELEFONO_SISTEMA = "sistema"
_EXT = {
    "application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif",
    "application/msword": "doc", "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.ms-excel": "xls", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "text/csv": "csv", "text/plain": "txt", "application/zip": "zip",
}
_ESTADOS = {
    "confirmado": "Pago confirmado", "pendiente_banco": "Esperando banco", "esperando_rut": "Faltaba RUT",
    "esperando_empresa": "Faltaba elegir empresa", "escalado": "Revisado por ejecutivo", "no_legible": "No comprobante / ilegible",
    "duplicado": "Reenvío", "recibido": "Recibido",
}


def reglas() -> dict:
    conf = config.negocio().get("archivos") or {}
    return {
        "dias_conservacion": int(conf.get("dias_conservacion") or 180),
        "dias_respaldo": int(conf.get("dias_respaldo") or 170),
        "correo": str(conf.get("correo_respaldo") or "alertas@huno.cl"),
    }


def nombre_archivo(c: Comprobante) -> str:
    """Mismo nombre que usa app.huno.cl al descargar: comprobante_AAAA-MM-DD_<tel>_<id>.<ext>"""
    mime = c.mime or "application/octet-stream"
    ext = _EXT.get(mime) or (mime.split("/")[-1][:8] or "bin")
    fecha = como_utc(c.creado_en).astimezone(horario.ahora_chile().tzinfo).date().isoformat() if c.creado_en else "sin-fecha"
    prefijo = "comprobante" if mime == "application/pdf" or mime.startswith("image/") else "archivo"
    return f"{prefijo}_{fecha}_{(c.telefono or '')[-8:]}_{c.id}.{ext}"


async def _empresas(telefono: str) -> str:
    try:
        return ", ".join(c.nombre for c in await huno.clientes_de_telefono(telefono)) or "Sin ficha"
    except Exception:  # noqa: BLE001
        return "?"


async def _html(lote: list[Comprobante], dias: int) -> str:
    filas = []
    for c in lote:
        fecha = como_utc(c.creado_en).astimezone(horario.ahora_chile().tzinfo).strftime("%d-%m-%Y %H:%M")
        borra = (como_utc(c.creado_en) + timedelta(days=dias)).astimezone(horario.ahora_chile().tzinfo).strftime("%d-%m-%Y")
        filas.append(
            "<tr>" + "".join(f"<td style='padding:4px 8px;border-bottom:1px solid #eee'>{escape(str(v))}</td>" for v in (
                fecha, f"+{c.telefono}", await _empresas(c.telefono),
                formatear_monto(c.monto) if c.monto else "—", _ESTADOS.get(c.estado, c.estado), nombre_archivo(c), borra,
            )) + "</tr>")
    cabecera = "".join(f"<th style='padding:4px 8px;text-align:left;background:#eef6fb'>{t}</th>" for t in (
        "Recibido", "WhatsApp", "Cliente", "Monto leído", "Estado", "Archivo adjunto", "Se borra del sistema"))
    return (
        "<div style='font-family:Arial,sans-serif;font-size:13px;color:#0a2a3a'>"
        f"<p>Respaldo automático de {len(lote)} archivo(s) recibido(s) por el WhatsApp de USP (+56 9 7870 1038).</p>"
        f"<p>Los archivos se conservan {dias} días en el sistema. Estos cumplieron {reglas()['dias_respaldo']} días: "
        "van adjuntos para que queden guardados en este correo.</p>"
        f"<table style='border-collapse:collapse;font-size:12px'><tr>{cabecera}</tr>{''.join(filas)}</table>"
        "<p style='color:#888;font-size:11px'>Enviado por el agente de WhatsApp USP de HUNO®.</p></div>"
    )


async def _enviar(asunto: str, html: str, adjuntos: list[dict]) -> tuple[bool, str]:
    token = (os.getenv("RESPALDO_TOKEN") or "").strip()
    if not token:
        return False, "Falta RESPALDO_TOKEN en Railway"
    try:
        async with httpx.AsyncClient(timeout=120.0) as cliente:
            r = await cliente.post(RESPALDO_URL, json={"asunto": asunto, "html": html, "archivos": adjuntos},
                                   headers={"X-USP-Token": token})
    except httpx.HTTPError as e:
        return False, f"No se pudo contactar {RESPALDO_URL}: {e}"
    try:
        datos = r.json()
    except ValueError:
        datos = {}
    if r.status_code == 200 and datos.get("ok"):
        return True, "ok"
    return False, f"HTTP {r.status_code}: {(datos.get('error') if datos else r.text[:200]) or 'sin detalle'}"


def _lotes(filas: list[tuple[int, int]]) -> list[list[int]]:
    """Agrupa (id, bytes) en lotes de hasta LOTE_BYTES (un archivo mas grande va solo)."""
    lotes, actual, tam = [], [], 0
    for cid, n in filas:
        if actual and tam + n > LOTE_BYTES:
            lotes.append(actual)
            actual, tam = [], 0
        actual.append(cid)
        tam += n
    if actual:
        lotes.append(actual)
    return lotes


async def correo_de_prueba() -> tuple[bool, str]:
    r = reglas()
    return await _enviar("Prueba de respaldo — WhatsApp USP",
                         f"<p>Prueba de configuración: el agente puede enviar respaldos a {escape(r['correo'])}.</p>", [])


async def respaldar_y_limpiar() -> dict:
    r = reglas()
    momento = ahora()
    limite_respaldo = momento - timedelta(days=r["dias_respaldo"])
    limite_borrado = momento - timedelta(days=r["dias_conservacion"])

    # 1. Respaldar por correo los que cumplieron dias_respaldo
    async with async_session() as s:
        pendientes = (await s.execute(
            select(Comprobante.id, func.length(Comprobante.archivo))
            .where(Comprobante.archivo.is_not(None), Comprobante.respaldado_en.is_(None),
                   Comprobante.creado_en < limite_respaldo)
            .order_by(Comprobante.id)
        )).all()
    respaldados, errores = 0, []
    for ids in _lotes([(cid, int(n or 0)) for cid, n in pendientes]):
        async with async_session() as s:
            lote = list((await s.execute(select(Comprobante).where(Comprobante.id.in_(ids)).order_by(Comprobante.id)))
                        .scalars().all())
        adjuntos = [{"nombre": nombre_archivo(c), "mime": c.mime or "application/octet-stream",
                     "base64": base64.b64encode(c.archivo).decode()} for c in lote if c.archivo]
        fechas = sorted(como_utc(c.creado_en).astimezone(horario.ahora_chile().tzinfo) for c in lote)
        desde, hasta = fechas[0].strftime("%d-%m-%Y"), fechas[-1].strftime("%d-%m-%Y")
        asunto = f"Respaldo WhatsApp USP — {len(lote)} archivo(s) recibidos " + (
            f"el {desde}" if desde == hasta else f"del {desde} al {hasta}")
        ok, detalle = await _enviar(asunto, await _html(lote, r["dias_conservacion"]), adjuntos)
        if not ok:
            errores.append(detalle)
            logger.error(f"No se pudo enviar el respaldo de {len(lote)} archivo(s): {detalle}")
            break  # se reintenta en la proxima vuelta
        async with async_session() as s:
            await s.execute(update(Comprobante).where(Comprobante.id.in_([c.id for c in lote]))
                            .values(respaldado_en=momento))
            await s.commit()
        respaldados += len(lote)

    # 2. Borrar el archivo (no los datos) de los que cumplieron dias_conservacion Y ya estan respaldados
    async with async_session() as s:
        borrado = await s.execute(
            update(Comprobante)
            .where(Comprobante.archivo.is_not(None), Comprobante.respaldado_en.is_not(None),
                   Comprobante.creado_en < limite_borrado)
            .values(archivo=None)
        )
        sin_respaldo = (await s.execute(
            select(func.count()).select_from(Comprobante)
            .where(Comprobante.archivo.is_not(None), Comprobante.respaldado_en.is_(None),
                   Comprobante.creado_en < limite_borrado)
        )).scalar_one()
        await s.commit()
    borrados = borrado.rowcount or 0

    # 3. Si hay archivos vencidos sin respaldo, que alguien lo revise (no se borran)
    if sin_respaldo:
        await escalamientos.crear(
            TELEFONO_SISTEMA, "error_tecnico",
            f"Hay archivos de WhatsApp con más de {r['dias_conservacion']} días que no se pudieron respaldar por correo "
            f"a {r['correo']}, así que NO se borraron. Revisar RESPALDO_TOKEN en Railway y huno.cl/api/usp_respaldo.php. "
            f"Último error: {errores[-1] if errores else 'sin envío reciente'}")

    salida = {"respaldados": respaldados, "borrados": borrados, "sin_respaldo_vencidos": sin_respaldo,
              "pendientes_de_respaldo": len(pendientes) - respaldados, "errores": errores}
    if respaldados or borrados or errores:
        logger.info(f"Respaldo de archivos: {salida}")
    return salida
