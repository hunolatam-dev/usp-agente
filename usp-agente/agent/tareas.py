# agent/tareas.py — Revision periodica (cada 30 min) para que nada quede esperando para siempre
"""
1. Comprobantes "pendiente_banco": se vuelven a cruzar con el banco. Si aparecio el pago, se
   confirma y se avisa; si paso el plazo (reglas.dias_espera_banco), conciliacion los deriva.
   Asi no depende de que alguien suba la cartola para que el caso llegue a una persona.
2. Comprobantes esperando RUT o empresa: si el cliente no respondio en
   reglas.horas_sin_respuesta, se abre un caso para un ejecutivo. Si despues responde y el pago
   se confirma, el caso se cierra solo.
3. Pagos confirmados que no se pudieron avisar (WhatsApp cerro la ventana de 24 h): si el
   cliente volvio a escribir, se avisan ahora.
Una vez al dia (desde las 7:00): respaldo por correo y borrado de archivos antiguos (respaldo.py).
"""

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import or_, select

from agent import avisos, conciliacion, config, escalamientos, horario, memoria, respaldo
from agent.db import Comprobante, Escalamiento, ahora, async_session
from agent.utils import formatear_monto

logger = logging.getLogger("usp")

INTERVALO = 30 * 60


async def revisar(proveedor) -> dict:
    reglas = config.reglas()

    # 1. Esperando el banco
    reevaluados = await conciliacion.reevaluar_pendientes(estados=("pendiente_banco",))
    aviso_banco = await avisos.avisar_confirmaciones(proveedor, reevaluados)

    # 2. Esperando una respuesta del cliente
    limite = ahora() - timedelta(hours=float(reglas.get("horas_sin_respuesta") or 6))
    async with async_session() as s:
        ya_derivado = select(Escalamiento.id).where(Escalamiento.comprobante_id == Comprobante.id,
                                                    Escalamiento.motivo == "cliente_no_respondio")
        esperando = (await s.execute(
            select(Comprobante).where(Comprobante.estado.in_(("esperando_rut", "esperando_empresa")),
                                      Comprobante.actualizado_en < limite, ~ya_derivado.exists())
        )).scalars().all()
    derivados = 0
    for c in esperando:
        falta = "el RUT de la empresa que paga" if c.estado == "esperando_rut" else "a cuál de sus empresas corresponde"
        await escalamientos.crear(
            c.telefono, "cliente_no_respondio",
            f"Comprobante #{c.id} por {formatear_monto(c.monto) if c.monto else 'monto ?'} del {c.fecha or '?'}: "
            f"el cliente no indicó {falta}. Si responde, el agente lo confirma solo.", c.id)
        derivados += 1

    # 3. Confirmados sin avisar (solo los que confirmo el agente; los manuales los avisa el ejecutivo)
    async with async_session() as s:
        sin_avisar = (await s.execute(
            select(Comprobante).where(Comprobante.estado == "confirmado", Comprobante.notificado.is_(False),
                                      Comprobante.actualizado_en >= ahora() - timedelta(days=15),
                                      or_(Comprobante.motivo.is_(None), ~Comprobante.motivo.like("manual%")))
        )).scalars().all()
    pendientes = []
    for c in sin_avisar:
        if memoria.dentro_de_ventana(await memoria.obtener_conversacion(c.telefono)):
            pendientes.append((c, await conciliacion.evaluar(c.id)))
    aviso_tardio = await avisos.avisar_confirmaciones(proveedor, pendientes)

    salida = {"reevaluados_banco": len(reevaluados), "avisos_banco": aviso_banco,
              "derivados_sin_respuesta": derivados, "avisos_tardios": aviso_tardio}
    if reevaluados or derivados or pendientes:
        logger.info(f"Revision periodica: {salida}")
    return salida


HORA_TAREAS_DIARIAS = 7  # hora de Chile desde la que corren las tareas del dia


async def diarias(dia_hecho):
    """Una vez al dia: respaldo/borrado de archivos antiguos y limpieza de eventos. Devuelve el dia hecho."""
    momento = horario.ahora_chile()
    if dia_hecho == momento.date() or momento.hour < HORA_TAREAS_DIARIAS:
        return dia_hecho
    await respaldo.respaldar_y_limpiar()
    await memoria.limpiar_eventos_viejos()
    return momento.date()


async def ciclo(proveedor):
    """Corre para siempre en segundo plano (main.lifespan)."""
    dia_hecho = None
    while True:
        await asyncio.sleep(INTERVALO)
        try:
            await revisar(proveedor)
        except Exception:  # noqa: BLE001
            logger.exception("Fallo la revision periodica")
        try:
            dia_hecho = await diarias(dia_hecho)
        except Exception:  # noqa: BLE001
            logger.exception("Fallaron las tareas diarias")
