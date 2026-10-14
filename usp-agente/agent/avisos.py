# agent/avisos.py — Avisar al cliente cuando un pago en espera se confirma despues
import logging

from agent import memoria
from agent.db import Comprobante, async_session
from agent.providers.base import ProveedorWhatsApp

logger = logging.getLogger("usp")


async def avisar_confirmaciones(proveedor: ProveedorWhatsApp | None, resultados) -> dict:
    """
    resultados: lista (Comprobante, Resultado) de conciliacion.reevaluar_pendientes.
    WhatsApp solo deja escribir texto libre dentro de 24 h desde el ultimo mensaje del
    cliente. Fuera de esa ventana el aviso queda pendiente en el panel (notificado=False).
    """
    enviados, sin_ventana = 0, 0
    for comp, res in resultados:
        if res.estado not in ("confirmado", "abono"):
            continue
        conv = await memoria.obtener_conversacion(comp.telefono)
        ok = False
        if proveedor and memoria.dentro_de_ventana(conv):
            ok = await proveedor.enviar_mensaje(
                comp.telefono, res.mensaje(), memoria.contexto_de(conv, idempotencia=f"confirmacion-{comp.id}-{res.folio}-{res.monto}")
            )
        if ok:
            enviados += 1
            async with async_session() as s:
                c = await s.get(Comprobante, comp.id)
                c.notificado = True
                await s.commit()
            await memoria.guardar_turno(comp.telefono, "[Sistema: el pago en espera se valido en el banco]", res.mensaje())
        else:
            sin_ventana += 1
    return {"avisados": enviados, "sin_avisar": sin_ventana}
