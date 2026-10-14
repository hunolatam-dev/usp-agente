# agent/cerebro.py — Responde mensajes de texto
"""
Primero reglas fijas (cero tokens): saludos, agradecimientos y RUT para comprobantes en
espera. Lo demas va a Claude con herramientas.
"""

import logging
import re

from sqlalchemy import func, select

from agent import conciliacion, config, herramientas, huno, llm, memoria
from agent.db import Comprobante, async_session
from agent.utils import buscar_ruts, sin_tildes

logger = logging.getLogger("usp")

MAX_VUELTAS = 4

_SALUDOS = {"hola", "buenas", "buenos dias", "buen dia", "buenas tardes", "buenas noches", "hola buenas",
            "hola buenos dias", "hola buenas tardes", "alo", "holi", "hello"}
_GRACIAS = {"gracias", "muchas gracias", "ok gracias", "ok", "okay", "perfecto", "perfecto gracias",
            "listo", "listo gracias", "vale", "ya", "genial", "excelente", "dale", "de nada", "👍", "🙏"}


def _limpio(texto: str) -> str:
    t = sin_tildes(texto).strip()
    t = re.sub(r"[!¡?¿.,;:]+", " ", t)
    return " ".join(t.split())


async def _comprobantes_esperando_rut(telefono: str) -> int:
    async with async_session() as s:
        return (await s.execute(
            select(func.count()).select_from(Comprobante)
            .where(Comprobante.telefono == telefono, Comprobante.estado == "esperando_rut")
        )).scalar_one()


async def quien_es(telefono: str) -> tuple[str | None, list[str]]:
    """(nombre del dueno del numero, empresas) segun las fichas de app.huno.cl."""
    clientes = await huno.clientes_de_telefono(telefono)
    nombre = next((c.nombre_de(telefono) for c in clientes if c.nombre_de(telefono)), None)
    return nombre, [c.nombre for c in clientes]


async def _eleccion_pendiente(telefono: str, texto: str) -> str | None:
    """Si hay un comprobante esperando que elija la empresa, interpreta la respuesta."""
    async with async_session() as s:
        comp = (await s.execute(
            select(Comprobante).where(Comprobante.telefono == telefono, Comprobante.estado == "esperando_empresa")
            .order_by(Comprobante.id)
        )).scalars().first()
        if comp is None:
            return None
        opciones = await conciliacion.opciones_empresa(s, comp)
    if not opciones:
        return None
    elegido = huno.elegir(texto, opciones)
    if elegido is None:
        # Solo se insiste si parece un intento de responder (corto); si no, sigue la conversacion normal
        if len(texto.strip()) <= 25:
            return config.respuesta("elegir_no_entendido", opciones=huno.lista_opciones(opciones))
        return None
    return (await conciliacion.elegir_empresa(comp.id, elegido.id)).mensaje()


async def regla_rapida(telefono: str, texto: str) -> str | None:
    eleccion = await _eleccion_pendiente(telefono, texto)
    if eleccion:
        return eleccion
    t = _limpio(texto)
    if t in _SALUDOS:
        nombre, _ = await quien_es(telefono)
        if nombre and config.respuesta("saludo_conocido"):
            return config.respuesta("saludo_conocido", nombre=nombre.split()[0].title())
        return config.respuesta("saludo")
    if t in _GRACIAS or texto.strip() in _GRACIAS:
        return config.respuesta("gracias")

    ruts = buscar_ruts(texto)
    if ruts and await _comprobantes_esperando_rut(telefono):
        r = await herramientas.registrar_rut_empresa(telefono, ruts[0])
        partes = [config.respuesta("rut_registrado", rut=r["rut"])] + r["comprobantes_reevaluados"]
        return "\n\n".join(partes)
    if not ruts and re.search(r"\d{7,8}\s*-\s*[\dkK]\b", texto) and await _comprobantes_esperando_rut(telefono):
        return config.respuesta("rut_invalido", rut=re.search(r"[\d.]{7,11}\s*-\s*[\dkK]", texto).group(0))
    return None


def _texto(respuesta) -> str:
    return "\n".join(b.text for b in respuesta.content if b.type == "text" and b.text).strip()


async def responder(telefono: str, texto: str) -> tuple[str, bool]:
    """Devuelve (respuesta, se_guarda_en_historial)."""
    rapida = await regla_rapida(telefono, texto)
    if rapida:
        return rapida, True

    historial = await memoria.obtener_historial(telefono)
    mensajes = historial + [{"role": "user", "content": texto}]
    # El system prompt va con cache: se cobra completo una vez y despues a ~10%
    system = [{"type": "text", "text": config.system_prompt(), "cache_control": {"type": "ephemeral"}}]
    nombre, empresas = await quien_es(telefono)
    if empresas:
        system.append({"type": "text", "text": (
            "## Quien escribe\nEste numero esta registrado en HUNO"
            + (f" a nombre de {nombre}" if nombre else "")
            + f", contacto de: {', '.join(empresas)}. Puedes usar consultar_facturas_pendientes."
            + (" Tiene VARIAS empresas: antes de mostrar facturas pregunta de cual." if len(empresas) > 1 else ""))})
    else:
        system.append({"type": "text", "text": (
            "## Quien escribe\nEste numero NO esta registrado en ninguna ficha de cliente de HUNO. "
            "No muestres deudas; si las pide, deriva con motivo registro_contacto.")})

    try:
        for _ in range(MAX_VUELTAS):
            r = await llm.crear_mensaje(model=llm.MODELO_CHAT, system=system, tools=herramientas.DEFINICIONES,
                                        messages=mensajes)
            if r.stop_reason != "tool_use":
                salida = _texto(r)
                return (salida, True) if salida else (config.respuesta("error"), False)
            mensajes.append({"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in r.content]})
            resultados = []
            for b in r.content:
                if b.type == "tool_use":
                    logger.info(f"Herramienta {b.name} para {telefono}")
                    resultados.append({"type": "tool_result", "tool_use_id": b.id,
                                       "content": await herramientas.ejecutar(b.name, b.input or {}, telefono)})
            mensajes.append({"role": "user", "content": resultados})
        logger.warning("Demasiadas vueltas de herramientas; se deriva")
        await herramientas.escalar_a_persona(telefono, "consulta", f"El agente no logro responder: {texto[:500]}")
        return config.respuesta("escalado_texto"), True
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error con Claude: {e}")
        await herramientas.escalar_a_persona(telefono, "consulta", f"Error tecnico respondiendo: {texto[:500]}")
        return config.respuesta("error"), False
