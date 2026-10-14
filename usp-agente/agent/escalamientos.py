# agent/escalamientos.py — Derivar casos a una persona de USP
import logging
import os

import httpx
from sqlalchemy import select

from agent.db import Escalamiento, async_session

logger = logging.getLogger("usp")

MOTIVOS = {
    "pedido_cliente": "El cliente pidió hablar con una persona",
    "registro_contacto": "Teléfono no registrado: validar a qué cliente pertenece",
    "consulta": "Consulta que el agente no puede resolver",
    "reclamo": "Reclamo o molestia del cliente",
    "monto_alto": "Monto sobre el máximo de confirmación automática",
    "cuenta_destino_distinta": "La cuenta de destino del comprobante no es de HUNO",
    "comprobante_reutilizado": "Comprobante ya usado para otro pago (posible fraude)",
    "sin_factura_exacta": "Ninguna factura pendiente coincide con el monto (pago parcial o de varias)",
    "factura_ambigua": "El monto coincide con facturas de varias empresas",
    "cliente_no_encontrado": "El RUT indicado no está en la base de clientes",
    "movimiento_ambiguo": "Varias transferencias del mismo monto en el banco; no se puede saber cuál es",
    "movimiento_rut_distinto": "La transferencia del banco viene de otro RUT",
    "sin_movimiento": "El pago no aparece en el banco después del plazo de espera",
    "conflicto": "La factura o el movimiento cambió mientras se confirmaba",
    "error_lectura": "No se pudo descargar o leer el archivo",
}


async def crear(telefono: str, motivo: str, resumen: str = "", comprobante_id: int | None = None) -> Escalamiento:
    async with async_session() as s:
        # No duplicar: si ya hay uno abierto por el mismo motivo y comprobante, se reutiliza
        q = select(Escalamiento).where(
            Escalamiento.telefono == telefono,
            Escalamiento.motivo == motivo,
            Escalamiento.estado == "abierto",
        )
        q = q.where(Escalamiento.comprobante_id == comprobante_id) if comprobante_id else q.where(
            Escalamiento.comprobante_id.is_(None)
        )
        existente = (await s.execute(q)).scalars().first()
        if existente:
            if resumen and resumen not in (existente.resumen or ""):
                existente.resumen = ((existente.resumen or "") + "\n" + resumen).strip()[-4000:]
                await s.commit()
            return existente
        esc = Escalamiento(telefono=telefono, motivo=motivo, resumen=resumen[:4000], comprobante_id=comprobante_id)
        s.add(esc)
        await s.commit()
    await notificar(esc)
    return esc


async def notificar(esc: Escalamiento):
    """Aviso opcional por Telegram (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID)."""
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    texto = (
        f"USP: caso #{esc.id} — {MOTIVOS.get(esc.motivo, esc.motivo)}\n"
        f"WhatsApp: +{esc.telefono}\n{esc.resumen[:800]}"
    )
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            await c.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat, "text": texto})
    except httpx.HTTPError as e:
        logger.warning(f"No se pudo avisar por Telegram: {e}")
