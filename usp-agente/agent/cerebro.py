# agent/cerebro.py — Responde mensajes de texto
"""
Primero reglas fijas (cero tokens): eleccion de empresa de un comprobante, saludos,
agradecimientos, RUT para comprobantes en espera y preguntas frecuentes muy claras (datos
de transferencia, horario, servicios, hablar con una persona). Lo demas va a Claude con
herramientas, con el contexto de quien escribe, sus casos y comprobantes en espera.
"""

import logging
import re
import time
from collections import defaultdict, deque

from sqlalchemy import func, select

from agent import conciliacion, config, escalamientos, herramientas, horario, huno, llm, memoria, registro
from agent.db import Comprobante, async_session, como_utc
from agent.utils import formatear_monto, rut_en_mensaje, sin_tildes

logger = logging.getLogger("usp")

MAX_VUELTAS = 4
MAX_LARGO_ENTRADA = 1500   # caracteres del mensaje que se mandan a la IA
MAX_LARGO_SALIDA = 3500    # WhatsApp corta en 4096

_SALUDOS = {"hola", "buenas", "buenos dias", "buen dia", "buenas tardes", "buenas noches", "hola buenas",
            "hola buenos dias", "hola buenas tardes", "hola buenas noches", "alo", "holi", "hello", "hi",
            "hola que tal", "saludos", "hola buen dia"}
_GRACIAS = {"gracias", "muchas gracias", "ok gracias", "ok muchas gracias", "perfecto gracias", "listo gracias",
            "vale gracias", "mil gracias", "gracias igualmente", "de nada", "🙏", "gracias 🙏", "muy amable"}
# Pueden ser un "gracias" o la respuesta a una pregunta del agente ("¿Quiere que lo derive?" -> "ok")
_AFIRMACIONES = {"ok", "okay", "oka", "okey", "perfecto", "listo", "vale", "ya", "genial", "excelente", "dale",
                 "bueno", "si", "sii", "👍", "👌", "✅", "ya po", "ok perfecto", "entendido"}

_RE_DATOS = re.compile(
    r"\b(datos (de )?(la )?(transferencia|cuenta|bancarios|para (transferir|pagar|depositar))|numero de (la )?cuenta|"
    r"a que cuenta|cual es la cuenta|donde (transfiero|deposito|le transfiero)|como (pago|transfiero|puedo pagar)|"
    r"cuenta (para|de) (pago|pagar|transferir|transferencia|deposito))\b")
_RE_HORARIO = re.compile(
    r"\b(horarios?( de atencion)?|a que hora (abren|cierran|atienden)|hasta que hora (atienden|abren|estan)|"
    r"atienden (hoy|el sabado|los sabados|los domingos)|donde (estan|quedan|se ubican)( las oficinas)?|"
    r"direccion de (la|las|sus) oficinas?)\b")
_RE_SERVICIOS = re.compile(
    r"\b(que servicios|servicios (que|de huno|ofrecen|tienen|prestan)|cuanto (cuesta|vale|sale|cobran)|precios?|"
    r"valor(es)? (del|de los|de un|de sus) (plan|planes|servicio|servicios)|planes|cotizar|cotizacion|"
    r"quiero contratar|contratar (un|el|sus) (plan|servicio))\b")
_RE_NO_SERVICIOS = re.compile(r"\b(factura|deuda|debo|pague|pago|cobro|cobraron|boleta|saldo|baja|cambi|cancel|"
                              r"termin|mi plan|mis planes|mi servicio|mis servicios)")
# Si el mensaje trae un problema o un matiz, no se contesta con una respuesta fija
_RE_MATIZ = re.compile(r"\b(pero|no puedo|problema|error|equivoq|reclamo|cuota|cuotas|tarjeta|efectivo|cheque|"
                       r"parcial|abono|parte|trabajador|turno|jornada|empleado)")
_RE_PERSONA = re.compile(
    r"^(hola )?((quiero|necesito|puedo|podria|me gustaria) )?(hablar|conversar|comunicarme|contactarme|contactar) "
    r"con (una persona|un ejecutivo|una ejecutiva|alguien|un humano|un agente|una persona real)( por favor)?$|"
    r"^(ejecutivo|ejecutiva|persona|humano|una persona|un ejecutivo)( por favor)?$")
# Un numero no registrado que pregunta por su cuenta: se le ofrece registrarlo (sin IA)
_RE_SU_CUENTA = re.compile(r"\b(debo|debemos|deuda|deudas|adeudo|facturas?|saldo|estado de cuenta|pendientes?|mis pagos|"
                           r"mi cuenta|nuestra cuenta|vencid[ao]s?)\b")
_RE_NINGUNA = re.compile(r"\b(ninguna|ninguno|otra empresa|otra|no es (ninguna|esa|esa empresa)|no corresponde)\b")

_uso_ia: dict[str, deque] = defaultdict(deque)


def _limpio(texto: str) -> str:
    t = sin_tildes(texto).strip()
    t = re.sub(r"[!¡?¿.,;:]+", " ", t)
    return " ".join(t.split())


async def _comprobante_en_espera(telefono: str, estado: str) -> Comprobante | None:
    async with async_session() as s:
        return (await s.execute(
            select(Comprobante).where(Comprobante.telefono == telefono, Comprobante.estado == estado)
            .order_by(Comprobante.id)
        )).scalars().first()


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


async def _pregunta_empresa(telefono: str) -> tuple[Comprobante, list[huno.Cliente], list[str]] | None:
    """Comprobante esperando que el cliente elija empresa, sus opciones y los textos con que se le pregunto."""
    comp = await _comprobante_en_espera(telefono, "esperando_empresa")
    if comp is None:
        return None
    async with async_session() as s:
        opciones = await conciliacion.opciones_empresa(s, comp)
    if not opciones:
        return None
    lista = huno.lista_opciones(opciones)
    textos = [conciliacion.Resultado("esperando_empresa", monto=comp.monto, opciones=lista).mensaje(),
              config.respuesta("elegir_no_entendido", opciones=lista),
              config.respuesta("recordar_empresa", monto=formatear_monto(comp.monto or 0), opciones=lista)]
    return comp, opciones, textos


async def _eleccion_pendiente(telefono: str, texto: str) -> str | None:
    """
    Si lo ultimo que pregunto el agente fue "¿a cual empresa corresponde el pago?", interpreta la
    respuesta. Si la conversacion siguio por otro lado, decide la IA (tiene la herramienta).
    """
    pendiente = await _pregunta_empresa(telefono)
    if pendiente is None:
        return None
    comp, opciones, textos = pendiente
    # Se interpreta como respuesta si lo ultimo que dijo el agente fue la pregunta, o si no ha dicho
    # nada despues de hacerla (p. ej. el historial no alcanzo a guardarse)
    ultimo = await memoria.ultimo_del_asistente(telefono, despues_de=comp.actualizado_en)
    if ultimo and not any(t and t in ultimo for t in textos):
        return None
    elegido = huno.elegir(texto, opciones)
    if elegido is not None:
        res = await conciliacion.elegir_empresa(comp.id, elegido.id)
        return res.mensaje()
    t = _limpio(texto)
    if _RE_NINGUNA.search(t):
        res = await conciliacion.escalar(comp.id, "factura_ambigua",
                                         f"El cliente dice que no corresponde a ninguna de las opciones: {texto[:300]}")
        return res.mensaje()
    if t in _SALUDOS or t in _GRACIAS or "?" in texto or len(texto.strip()) > 25:
        return None
    return textos[1]


async def _recordatorio(telefono: str) -> str:
    """Si hay un comprobante esperando una respuesta del cliente, se le recuerda."""
    pendiente = await _pregunta_empresa(telefono)
    if pendiente:
        comp, _, textos = pendiente
        return textos[2]
    comp = await _comprobante_en_espera(telefono, "esperando_rut")
    if comp:
        return config.respuesta("recordar_rut", monto=formatear_monto(comp.monto or 0))
    return ""


def _con_recordatorio(texto: str, recordatorio: str) -> str:
    return f"{texto}\n\n{recordatorio}" if recordatorio else texto


async def _registro(telefono: str, texto: str, registrado: bool) -> str | None:
    """Paso del registro del numero (RUT + correo, nombre o codigo). Si trae un RUT y hay comprobantes
    esperando RUT, ese RUT tambien se usa para ellos."""
    respuesta = await registro.manejar(telefono, texto, registrado)
    if respuesta is None:
        return None
    ruts = [r for r in rut_en_mensaje(texto) if r != config.rut_propio()]
    if ruts and await _comprobantes_esperando_rut(telefono):
        r = await herramientas.registrar_rut_empresa(telefono, ruts[0])
        return "\n\n".join([config.respuesta("rut_registrado", rut=r["rut"])] + r["comprobantes_reevaluados"] + [respuesta])
    return respuesta


async def regla_rapida(telefono: str, texto: str) -> str | None:
    eleccion = await _eleccion_pendiente(telefono, texto)
    if eleccion:
        return eleccion
    nombre, empresas = await quien_es(telefono)
    registrado = bool(empresas)
    paso_registro = await _registro(telefono, texto, registrado)
    if paso_registro:
        return paso_registro
    t = _limpio(texto)
    if t in _SALUDOS:
        if nombre and config.respuesta("saludo_conocido"):
            saludo = config.respuesta("saludo_conocido", nombre=nombre.split()[0].title())
        else:
            saludo = config.respuesta("saludo")
        if not registrado and registro.es_telefono(telefono):
            recordar = await registro.recordatorio(telefono)
            saludo = _con_recordatorio(saludo, recordar or await registro.invitacion(telefono, "saludo_registro"))
        return _con_recordatorio(saludo, await _recordatorio(telefono))
    if t in _GRACIAS or texto.strip() in _GRACIAS:
        return _con_recordatorio(config.respuesta("gracias"), await _recordatorio(telefono))
    if t in _AFIRMACIONES or texto.strip() in _AFIRMACIONES:
        # "ok" despues de una pregunta del agente es una respuesta: la interpreta la IA
        if (await memoria.ultimo_del_asistente(telefono)).rstrip().endswith("?"):
            return None
        return _con_recordatorio(config.respuesta("gracias"), await _recordatorio(telefono))

    ruts = [r for r in rut_en_mensaje(texto) if r != config.rut_propio()]
    if ruts and await _comprobantes_esperando_rut(telefono):
        r = await herramientas.registrar_rut_empresa(telefono, ruts[0])
        partes = [config.respuesta("rut_registrado", rut=r["rut"])] + r["comprobantes_reevaluados"]
        return "\n\n".join(partes)
    if not ruts and re.search(r"\d{7,8}\s*-\s*[\dkK]\b", texto) and await _comprobantes_esperando_rut(telefono):
        return config.respuesta("rut_invalido", rut=re.search(r"[\d.]{7,11}\s*-\s*[\dkK]", texto).group(0))

    # Preguntas frecuentes inequivocas y cortas: respuesta fija
    if len(texto) <= 80 and not _RE_MATIZ.search(t):
        if _RE_PERSONA.search(t):
            await herramientas.escalar_a_persona(telefono, "pedido_cliente", f"Pidió hablar con una persona: {texto}")
            return config.respuesta("escalado_texto")
        if _RE_DATOS.search(t):
            return config.respuesta("datos_transferencia")
        if _RE_HORARIO.search(t):
            return config.respuesta("horario_atencion")
        if _RE_SERVICIOS.search(t) and not _RE_NO_SERVICIOS.search(t):
            clave = "servicios_cotizador" if str(config.negocio().get("cotizador_url") or "").strip() else "servicios"
            return config.respuesta(clave)
    if not registrado and registro.es_telefono(telefono) and len(texto) <= 120 and _RE_SU_CUENTA.search(t) \
            and not await _comprobantes_esperando_rut(telefono):
        return await registro.invitacion(telefono)
    return None


def _para_whatsapp(texto: str) -> str:
    """WhatsApp no entiende markdown: **negrita** -> *negrita*, sin titulos #."""
    t = re.sub(r"\*\*(.+?)\*\*", r"*\1*", texto or "")
    t = re.sub(r"^#{1,6}\s*", "", t, flags=re.M)
    t = t.strip()
    if len(t) > MAX_LARGO_SALIDA:
        t = t[:MAX_LARGO_SALIDA].rsplit("\n", 1)[0] + "\n…"
    return t


def _texto(respuesta) -> str:
    return _para_whatsapp("\n".join(b.text for b in respuesta.content if b.type == "text" and b.text))


def _excede_limite(telefono: str) -> bool:
    maximo = int(config.reglas().get("max_consultas_ia_hora") or 30)
    marcas = _uso_ia[telefono]
    ahora = time.monotonic()
    while marcas and ahora - marcas[0] > 3600:
        marcas.popleft()
    if len(marcas) >= maximo:
        return True
    marcas.append(ahora)
    return False


def _fecha_corta(dt) -> str:
    try:
        return como_utc(dt).astimezone(horario.ahora_chile().tzinfo).strftime("%d-%m %H:%M")
    except Exception:  # noqa: BLE001
        return "?"


async def contexto(telefono: str) -> str:
    """Bloque 'Quien escribe' para la IA (no va en cache: cambia en cada mensaje)."""
    lineas = ["## Quien escribe", horario.contexto()]
    nombre, empresas = await quien_es(telefono)
    if empresas:
        lineas.append(
            "Este numero esta registrado en HUNO" + (f" a nombre de {nombre}" if nombre else "")
            + f", contacto de: {', '.join(empresas)}. Puedes usar consultar_facturas_pendientes."
            + (" Tiene VARIAS empresas: antes de mostrar facturas pregunta de cual." if len(empresas) > 1 else ""))
    else:
        lineas.append("Este numero NO esta registrado en ninguna ficha de cliente de HUNO. No muestres deudas; "
                      "si las pide, sigue la regla 2 (ofrecer registrar el numero). Puede ser alguien que aun no es cliente.")
    en_curso = await registro.contexto(telefono)
    if en_curso:
        lineas.append(en_curso)
    casos = await escalamientos.abiertos(telefono)
    if casos:
        lineas.append("Casos abiertos con un ejecutivo (ya derivados): " + "; ".join(
            f"{escalamientos.MOTIVOS.get(c.motivo, c.motivo)} (desde {_fecha_corta(c.creado_en)})" for c in casos))
    async with async_session() as s:
        espera = (await s.execute(
            select(Comprobante).where(Comprobante.telefono == telefono,
                                      Comprobante.estado.in_(("esperando_empresa", "esperando_rut", "pendiente_banco")))
            .order_by(Comprobante.id.desc()).limit(3)
        )).scalars().all()
    for c in espera:
        monto = formatear_monto(c.monto) if c.monto else "monto no leído"
        if c.estado == "esperando_empresa":
            pendiente = await _pregunta_empresa(telefono)
            opciones = huno.lista_opciones(pendiente[1]) if pendiente else ""
            lineas.append(f"Comprobante por {monto} esperando que el cliente indique la empresa. Opciones:\n{opciones}")
        elif c.estado == "esperando_rut":
            lineas.append(f"Comprobante por {monto} esperando el RUT de la empresa que paga (usa registrar_rut_empresa).")
        else:
            lineas.append(f"Comprobante por {monto} recibido, esperando que se refleje en el banco.")
    return "\n".join(lineas)


async def responder(telefono: str, texto: str, previo: str = "") -> tuple[str, bool]:
    """Devuelve (respuesta, se_guarda_en_historial). Siempre devuelve algo para el cliente."""
    try:
        rapida = None if previo else await regla_rapida(telefono, texto)
        if rapida:
            return rapida, True
        if _excede_limite(telefono):
            logger.warning(f"{telefono}: supero el maximo de consultas a la IA por hora")
            await herramientas.escalar_a_persona(telefono, "consulta",
                                                 f"Muchos mensajes en una hora; ultimo: {texto[:300]}")
            return config.respuesta("limite"), True

        contenido = texto[:MAX_LARGO_ENTRADA] + (" […]" if len(texto) > MAX_LARGO_ENTRADA else "")
        if previo:
            contenido = f"{previo}\n{contenido}"
        historial = await memoria.obtener_historial(telefono)
        mensajes = memoria.fusionar(historial + [{"role": "user", "content": contenido}])
        # El system prompt va con cache: se cobra completo una vez y despues a ~10%
        system = [{"type": "text", "text": config.system_prompt(), "cache_control": {"type": "ephemeral"}},
                  {"type": "text", "text": await contexto(telefono)}]

        for _ in range(MAX_VUELTAS):
            r = await llm.crear_mensaje(model=llm.MODELO_CHAT, system=system, tools=herramientas.DEFINICIONES,
                                        messages=mensajes)
            if r.stop_reason != "tool_use":
                salida = _texto(r)
                if salida:
                    return salida, True
                logger.warning(f"La IA no devolvio texto (stop_reason={r.stop_reason})")
                await herramientas.escalar_a_persona(telefono, "error_tecnico", f"La IA no respondió a: {texto[:500]}")
                return config.respuesta("error"), False
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
        logger.exception(f"Error respondiendo a {telefono}: {e}")
        try:
            await herramientas.escalar_a_persona(telefono, "error_tecnico", f"Error técnico respondiendo: {texto[:500]}")
        except Exception:  # noqa: BLE001
            logger.exception("Tampoco se pudo crear el caso")
        return config.respuesta("error"), False
