# agent/memoria.py — Historial, deduplicacion de eventos y estado de conversaciones
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from agent.db import Conversacion, EventoProcesado, Mensaje, ahora, async_session, como_utc


async def marcar_evento(evento_id: str) -> bool:
    """True si es nuevo. La clave primaria garantiza que dos entregas simultaneas no pasen."""
    if not evento_id:
        return True
    async with async_session() as s:
        s.add(EventoProcesado(evento_id=evento_id, creado_en=ahora()))
        try:
            await s.commit()
            return True
        except IntegrityError:
            await s.rollback()
            return False


async def liberar_evento(evento_id: str):
    if not evento_id:
        return
    async with async_session() as s:
        await s.execute(delete(EventoProcesado).where(EventoProcesado.evento_id == evento_id))
        await s.commit()


async def limpiar_eventos_viejos(dias: int = 7):
    async with async_session() as s:
        await s.execute(delete(EventoProcesado).where(EventoProcesado.creado_en < ahora() - timedelta(days=dias)))
        await s.commit()


async def guardar_turno(telefono: str, usuario: str, asistente: str):
    async with async_session() as s:
        s.add(Mensaje(telefono=telefono, role="user", content=usuario, creado_en=ahora()))
        s.add(Mensaje(telefono=telefono, role="assistant", content=asistente, creado_en=ahora()))
        await s.commit()


async def guardar_mensaje(telefono: str, role: str, contenido: str):
    """Un mensaje suelto (lo que escribe el cliente mientras atiende un ejecutivo, o lo que responde el ejecutivo)."""
    if not (contenido or "").strip():
        return
    async with async_session() as s:
        s.add(Mensaje(telefono=telefono, role=role, content=contenido[:4000], creado_en=ahora()))
        await s.commit()


def fusionar(mensajes: list[dict]) -> list[dict]:
    """La API exige turnos alternados user/assistant: se juntan los consecutivos del mismo rol."""
    salida: list[dict] = []
    for m in mensajes:
        if salida and salida[-1]["role"] == m["role"] and isinstance(m["content"], str) \
                and isinstance(salida[-1]["content"], str):
            salida[-1] = {"role": m["role"], "content": salida[-1]["content"] + "\n" + m["content"]}
        else:
            salida.append(dict(m))
    return salida


async def obtener_historial(telefono: str, limite: int = 12) -> list[dict]:
    async with async_session() as s:
        filas = (
            await s.execute(
                select(Mensaje).where(Mensaje.telefono == telefono).order_by(Mensaje.id.desc()).limit(limite)
            )
        ).scalars().all()
    filas = list(reversed(filas))
    while filas and filas[0].role != "user":
        filas.pop(0)
    return fusionar([{"role": f.role, "content": f.content} for f in filas])


async def ultimo_del_asistente(telefono: str, despues_de=None) -> str:
    """Ultima respuesta guardada para este numero (opcionalmente, solo si es posterior a `despues_de`)."""
    async with async_session() as s:
        q = select(Mensaje).where(Mensaje.telefono == telefono, Mensaje.role == "assistant")
        fila = (await s.execute(q.order_by(Mensaje.id.desc()).limit(1))).scalars().first()
    if fila is None:
        return ""
    if despues_de is not None and como_utc(fila.creado_en) < como_utc(despues_de):
        return ""
    return fila.content or ""


async def registrar_entrante(telefono: str, contexto: dict, nombre: str | None) -> Conversacion:
    """Guarda como responderle a este telefono (conversation_id/account_id) y cuando escribio."""
    async with async_session() as s:
        conv = await s.get(Conversacion, telefono)
        if conv is None:
            conv = Conversacion(telefono=telefono)
            s.add(conv)
        if contexto.get("conversation_id"):
            conv.conversation_id = contexto["conversation_id"]
        if contexto.get("account_id"):
            conv.account_id = contexto["account_id"]
        if nombre:
            conv.nombre_whatsapp = nombre[:200]
        conv.ultimo_entrante_en = ahora()
        await s.commit()
        return conv


async def obtener_conversacion(telefono: str) -> Conversacion | None:
    async with async_session() as s:
        return await s.get(Conversacion, telefono)


async def segundos_desde_entrante(telefono: str, conversation_id: str | None = None) -> float | None:
    async with async_session() as s:
        conv = None
        if conversation_id:
            conv = (await s.execute(
                select(Conversacion).where(Conversacion.conversation_id == conversation_id)
            )).scalars().first()
        if conv is None and telefono:
            conv = await s.get(Conversacion, telefono)
    if conv is None or conv.ultimo_entrante_en is None:
        return None
    return (ahora() - como_utc(conv.ultimo_entrante_en)).total_seconds()


async def esta_pausada(telefono: str) -> bool:
    conv = await obtener_conversacion(telefono)
    return bool(conv and conv.pausado_hasta and como_utc(conv.pausado_hasta) > ahora())


async def pausar_por_humano(conversation_id: str, telefono: str, horas: int) -> bool:
    """Un ejecutivo respondio desde el celular: el agente se calla un rato."""
    async with async_session() as s:
        conv = None
        if conversation_id:
            conv = (
                await s.execute(select(Conversacion).where(Conversacion.conversation_id == conversation_id))
            ).scalars().first()
        if conv is None and telefono:
            conv = await s.get(Conversacion, telefono)
        if conv is None:
            return False
        conv.pausado_hasta = ahora() + timedelta(hours=horas)
        await s.commit()
        return True


async def reanudar(telefono: str):
    async with async_session() as s:
        conv = await s.get(Conversacion, telefono)
        if conv:
            conv.pausado_hasta = None
            await s.commit()


async def guardar_rut_declarado(telefono: str, rut: str):
    async with async_session() as s:
        conv = await s.get(Conversacion, telefono)
        if conv is None:
            conv = Conversacion(telefono=telefono)
            s.add(conv)
        conv.rut_declarado = rut
        await s.commit()


def contexto_de(conv: Conversacion | None, idempotencia: str | None = None) -> dict:
    if conv is None:
        return {}
    ctx = {"conversation_id": conv.conversation_id, "account_id": conv.account_id}
    if idempotencia:
        ctx["idempotencia"] = idempotencia
    return ctx


def dentro_de_ventana(conv: Conversacion | None, margen_horas: int = 23) -> bool:
    """WhatsApp solo permite texto libre hasta 24 h despues del ultimo mensaje del cliente."""
    if conv is None or conv.ultimo_entrante_en is None:
        return False
    return ahora() - como_utc(conv.ultimo_entrante_en) < timedelta(hours=margen_horas)
