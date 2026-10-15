# agent/registro.py — El cliente registra su numero de WhatsApp en la ficha de su empresa
"""
Un numero que no esta en ninguna ficha de app.huno.cl no puede ver facturas. El cliente puede
registrarlo el mismo, en tres pasos:

  1. Indica el RUT de la empresa y el correo que la empresa tiene registrado en HUNO®. Se cruza
     con app.huno.cl (clientes.rut y clientes.correos / clientes.portal_email).
  2. Si calzan, se le pide su nombre (el de quien habla).
  3. Se le envia el MISMO codigo del "Estado de cuenta" de huno.cl: la Edge Function send-otp
     (funcion ec_emitir_codigo) lo manda al correo registrado de la empresa. El cliente lo escribe
     en el chat y se valida con ec_verificar (5 intentos, vence en 10 minutos).

Con el codigo correcto, el numero queda en clientes.telefonos con el nombre de quien habla (mismo
formato que la app) y desde ahi el agente lo saluda por su nombre y le muestra las facturas de esa
empresa. Queda un registro en los casos resueltos de app.huno.cl.

Limites por numero en 24 h: 3 intentos de RUT/correo que no calzan y 3 envios de codigo. Ademas
la base aplica los suyos (ec_emitir_codigo: 3 por RUT cada 15 min). Al superarlos se deriva.
"""

import json
import logging
import os
import re
from datetime import datetime, timedelta

import httpx
from sqlalchemy import text

from agent import config, escalamientos, huno
from agent.db import Conversacion, ahora, async_session, como_utc, engine
from agent.utils import formatear_rut, normalizar_rut, rut_en_mensaje, sin_tildes

logger = logging.getLogger("usp")

MAX_FALLOS = 3
MAX_ENVIOS = 3
VIGENCIA = {"datos": timedelta(hours=24), "nombre": timedelta(minutes=30), "codigo": timedelta(minutes=30)}

_RE_CORREO = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_RE_PREFIJO_NOMBRE = re.compile(r"^(hola[\s,]*)?(mi nombre es|me llamo|soy|habla|le habla|les habla|nombre\s*:?)\s+", re.I)
_RE_NOMBRE = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ][A-Za-zÁÉÍÓÚÜÑáéíóúüñ' .-]{1,59}")
_RE_REENVIAR = re.compile(r"\b(nuevo codigo|otro codigo|reenvia|reenviar|reenvie|no (me )?llego|no ha llegado|no me ha llegado)")
_RE_CANCELAR = re.compile(r"^(cancelar|cancelar registro|no quiero registrar(lo)?|olvidalo)$")


# ── Estado del registro (usp_conversaciones.registro) ────────────────────


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _desde_iso(texto: str | None) -> datetime | None:
    try:
        return como_utc(datetime.fromisoformat(texto)) if texto else None
    except ValueError:
        return None


def _recientes(marcas: list | None, horas: int = 24) -> list[str]:
    limite = ahora() - timedelta(hours=horas)
    return [m for m in (marcas or []) if (_desde_iso(m) or limite) > limite]


async def estado(telefono: str) -> dict:
    """Paso en curso ({} si no hay). Un paso vencido se descarta, pero se recuerdan fallos y envios."""
    async with async_session() as s:
        conv = await s.get(Conversacion, telefono)
        crudo = conv.registro if conv else None
    try:
        est = json.loads(crudo) if crudo else {}
    except ValueError:
        est = {}
    paso, desde = est.get("paso"), _desde_iso(est.get("desde"))
    if paso and (desde is None or ahora() - desde > VIGENCIA.get(paso, timedelta(hours=24))):
        est = {"fallos": est.get("fallos", []), "envios": est.get("envios", [])}
    est["fallos"], est["envios"] = _recientes(est.get("fallos")), _recientes(est.get("envios"))
    return est


async def _guardar(telefono: str, est: dict | None):
    async with async_session() as s:
        conv = await s.get(Conversacion, telefono)
        if conv is None:
            conv = Conversacion(telefono=telefono)
            s.add(conv)
        conv.registro = json.dumps(est, ensure_ascii=False) if est else None
        await s.commit()


def _con_paso(est: dict, paso: str, **extra) -> dict:
    return {**est, **extra, "paso": paso, "desde": _iso(ahora())}


# ── Utilidades ────────────────────────────────────────────────────────────


def es_telefono(telefono: str) -> bool:
    """Solo numeros reales (no el id de usuario de WhatsApp sin numero visible)."""
    return telefono.isdigit() and 10 <= len(telefono) <= 15


def extraer_correo(texto: str | None) -> str | None:
    m = _RE_CORREO.search(texto or "")
    return m.group(0).lower().strip(".") if m else None


_NO_NOMBRE = {"hola", "buenas", "buenos", "tardes", "noches", "gracias", "ok", "okay", "si", "no", "ya", "listo",
              "dale", "vale", "perfecto", "consulta", "ayuda", "factura", "facturas", "pago", "pagos", "rut", "correo",
              "codigo", "empresa", "cancelar", "quiero", "necesito", "favor", "deuda", "debo"}


def limpiar_nombre(texto: str) -> str | None:
    t = _RE_PREFIJO_NOMBRE.sub("", (texto or "").strip()).strip(" .!¡,")
    if "?" in t or "@" in t or any(ch.isdigit() for ch in t) or not _RE_NOMBRE.fullmatch(t):
        return None
    if any(w in _NO_NOMBRE for w in sin_tildes(t).split()):
        return None  # "hola", "ok", "gracias"... no son un nombre
    palabras = t.split()
    if not 1 <= len(palabras) <= 6:
        return None
    particulas = {"de", "del", "la", "las", "los", "y", "da", "do", "dos", "van", "von"}
    return " ".join(p.lower() if i and p.lower() in particulas
                    else p if (not p.islower() and not p.isupper()) else p.capitalize()
                    for i, p in enumerate(palabras))


def _limpio(texto: str) -> str:
    return " ".join(re.sub(r"[!¡?¿.,;:]+", " ", sin_tildes(texto)).split())


def _conf() -> dict:
    return config.negocio().get("registro_whatsapp") or {}


# ── Codigo del Estado de cuenta (mismo que huno.cl) ──────────────────────


async def enviar_codigo(rut: str) -> tuple[bool, str]:
    """Pide a send-otp (la misma funcion que usa huno.cl) que mande el codigo. (ok, correo enmascarado | error)."""
    url = os.getenv("OTP_URL") or _conf().get("otp_url") or ""
    clave = os.getenv("SUPABASE_ANON_KEY") or _conf().get("supabase_anon_key") or ""
    if not url or not clave:
        return False, "falta configurar el envío del código"
    try:
        async with httpx.AsyncClient(timeout=30.0) as cliente:
            r = await cliente.post(url, json={"rut": formatear_rut(rut)},
                                   headers={"apikey": clave, "Authorization": f"Bearer {clave}"})
        datos = r.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.error(f"send-otp no respondio bien: {e}")
        return False, "no pudimos enviar el código"
    if datos.get("ok"):
        return True, str(datos.get("email_mask") or "el correo registrado")
    logger.warning(f"send-otp rechazo el envio: {datos}")
    return False, str(datos.get("error") or f"error {r.status_code}").rstrip(".")


async def verificar_codigo(rut: str, codigo: str) -> dict:
    """Valida con ec_verificar (la misma funcion del Estado de cuenta). Devuelve su respuesta."""
    if engine.dialect.name != "postgresql":
        return {"ok": False, "error": "no_disponible"}
    async with async_session() as s:
        crudo = (await s.execute(text("select public.ec_verificar(:r, :c)"),
                                 {"r": formatear_rut(rut), "c": codigo})).scalar()
        r = crudo if isinstance(crudo, dict) else json.loads(crudo or "{}")
        if r.get("ok") and r.get("token"):
            # ec_verificar abre una sesion web de 20 min que aqui no se usa: se cierra
            await s.execute(text("delete from public.ec_sesion where hash = encode(sha256(convert_to(:t, 'UTF8')), 'hex')"),
                            {"t": r["token"]})
        await s.commit()
    r.pop("token", None)
    return r


# ── Pasos ─────────────────────────────────────────────────────────────────


async def invitacion(telefono: str, clave: str = "registro_invitacion") -> str:
    """Le ofrece registrar el numero y deja el chat esperando RUT y correo."""
    est = await estado(telefono)
    if es_telefono(telefono) and est.get("paso") not in ("nombre", "codigo"):
        await _guardar(telefono, _con_paso(est, "datos"))
    return config.respuesta(clave)


async def _derivar(telefono: str, est: dict, detalle: str) -> str:
    await escalamientos.crear(telefono, "registro_contacto",
                              f"No se pudo registrar el número por WhatsApp: {detalle}. "
                              f"RUT indicado: {formatear_rut(est['rut']) if est.get('rut') else '-'}; "
                              f"correo: {est.get('correo') or '-'}; nombre: {est.get('nombre') or '-'}")
    await _guardar(telefono, {"fallos": est.get("fallos", []), "envios": est.get("envios", [])})
    return config.respuesta("registro_derivado")


async def validar_datos(telefono: str, rut: str, correo: str) -> str:
    est = await estado(telefono)
    if not es_telefono(telefono):
        return await _derivar(telefono, est, "WhatsApp no muestra el número de este contacto")
    if len(est["fallos"]) >= MAX_FALLOS:
        return await _derivar(telefono, est, "varios intentos con datos que no calzan")
    rut_n, correo_n = normalizar_rut(rut), (correo or "").strip().lower()
    elegido = None
    if rut_n and correo_n and rut_n != config.rut_propio():
        for c in await huno.clientes_de_rut(rut_n):
            if correo_n in await huno.correos_de(c.id):
                elegido = c
                break
    if elegido is None:
        est["fallos"].append(_iso(ahora()))
        restantes = MAX_FALLOS - len(est["fallos"])
        if restantes <= 0:
            return await _derivar(telefono, est, "varios intentos con datos que no calzan")
        await _guardar(telefono, _con_paso({k: v for k, v in est.items() if k in ("fallos", "envios")}, "datos"))
        return config.respuesta("registro_no_valido", restantes=restantes)
    if telefono in [t for t, _ in elegido.telefonos]:
        await _guardar(telefono, None)
        return config.respuesta("registro_ya_registrado", empresa=elegido.nombre)
    await _guardar(telefono, _con_paso(est, "nombre", rut=rut_n, correo=correo_n, cliente_id=elegido.id))
    return config.respuesta("registro_pedir_nombre")


async def _mandar_codigo(telefono: str, est: dict) -> str:
    if len(est["envios"]) >= MAX_ENVIOS:
        return await _derivar(telefono, est, "se pidió el código varias veces")
    ok, detalle = await enviar_codigo(est["rut"])
    est["envios"].append(_iso(ahora()))
    if not ok:
        await _guardar(telefono, _con_paso(est, "codigo", destino=None))
        return config.respuesta("registro_envio_fallo", detalle=detalle)
    await _guardar(telefono, _con_paso(est, "codigo", destino=detalle))
    return config.respuesta("registro_codigo_enviado", nombre=est["nombre"].split()[0], correo=detalle)


async def _recibir_codigo(telefono: str, est: dict, codigo: str) -> str:
    r = await verificar_codigo(est["rut"], codigo)
    if not r.get("ok"):
        error = r.get("error")
        if error == "codigo":
            return config.respuesta("registro_codigo_error", restantes=r.get("restantes", 0))
        if error == "limite":
            return config.respuesta("registro_espera")
        if error == "no_disponible":
            return await _derivar(telefono, est, "la validación del código no está disponible")
        return config.respuesta("registro_codigo_vencido")
    cliente = await huno.cliente_por_id(est["cliente_id"])
    await huno.agregar_telefono(est["cliente_id"], telefono, est["nombre"])
    await _guardar(telefono, None)
    empresa = cliente.nombre if cliente else "su empresa"
    await escalamientos.registrar_hecho(
        telefono, "registro_whatsapp",
        f"Número +{telefono} registrado en {empresa} ({formatear_rut(est['rut'])}) a nombre de {est['nombre']}. "
        f"Validado con el RUT, el correo registrado {est['correo']} y el código de Estado de cuenta enviado a "
        f"{est.get('destino') or 'el correo registrado'}.")
    partes = [config.respuesta("registro_listo", nombre=est["nombre"].split()[0], empresa=empresa)]
    # Comprobantes que esperaban saber de que empresa eran: ahora el numero ya es de una
    from agent import conciliacion

    for comp, res in await conciliacion.reevaluar_pendientes(telefono, estados=("esperando_rut",)):
        if res.estado == "confirmado":
            await conciliacion.marcar_notificado(comp.id)
        partes.append(res.mensaje())
    return "\n\n".join(partes)


async def manejar(telefono: str, texto: str, registrado: bool) -> str | None:
    """
    Si el mensaje es parte del registro (RUT y correo, el nombre o el codigo), lo atiende y
    devuelve la respuesta. None = no es del registro; sigue la conversacion normal.
    """
    est = await estado(telefono)
    paso, t = est.get("paso"), _limpio(texto)
    if paso and _RE_CANCELAR.match(t):
        await _guardar(telefono, {"fallos": est["fallos"], "envios": est["envios"]})
        return config.respuesta("registro_cancelado")

    if paso == "codigo":
        digitos = re.sub(r"\D", "", texto)
        if len(digitos) == 6 and len(texto.strip()) <= 30:
            return await _recibir_codigo(telefono, est, digitos)
        if _RE_REENVIAR.search(t):
            return await _mandar_codigo(telefono, est)
        return None
    if paso == "nombre":
        nombre = limpiar_nombre(texto)
        if nombre:
            est["nombre"] = nombre
            return await _mandar_codigo(telefono, est)
        return None if "?" in texto else config.respuesta("registro_nombre_invalido")

    if registrado and not paso:
        return None  # numero ya registrado: el RUT/correo que escriba es para otra cosa
    correo = extraer_correo(texto)
    ruts = [r for r in rut_en_mensaje(texto) if r != config.rut_propio()]
    if correo and ruts:
        return await validar_datos(telefono, ruts[0], correo)
    if paso == "datos" and (correo or ruts):
        rut, correo = (ruts[0] if ruts else est.get("rut")), (correo or est.get("correo"))
        if rut and correo:
            return await validar_datos(telefono, rut, correo)
        await _guardar(telefono, _con_paso(est, "datos", rut=rut, correo=correo))
        return config.respuesta("registro_falta_correo" if rut else "registro_falta_rut")
    return None


async def recordatorio(telefono: str) -> str:
    """Texto para recordar en que paso va el registro (se agrega al saludo)."""
    est = await estado(telefono)
    if est.get("paso") == "codigo" and est.get("destino"):
        return config.respuesta("registro_recordar_codigo", correo=est["destino"])
    if est.get("paso") == "nombre":
        return config.respuesta("registro_pedir_nombre")
    return ""


async def contexto(telefono: str) -> str:
    """Linea para la IA sobre el registro en curso."""
    est = await estado(telefono)
    paso = est.get("paso")
    if paso == "datos":
        return ("Registro del número en curso: falta que indique el RUT de la empresa y el correo registrado "
                "(usa registrar_telefono cuando los dé).")
    if paso == "nombre":
        return "Registro del número en curso: RUT y correo validados; falta que diga su nombre."
    if paso == "codigo":
        return (f"Registro del número en curso: se envió el código a {est.get('destino') or 'su correo'}; falta que "
                "lo escriba en el chat (6 dígitos). Si no le llegó, que escriba «nuevo código».")
    return ""
