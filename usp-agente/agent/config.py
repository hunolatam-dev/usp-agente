# agent/config.py — Carga config/business.yaml, config/prompts.yaml y knowledge/
import logging
import re
from functools import lru_cache
from pathlib import Path

import yaml

logger = logging.getLogger("usp")

RAIZ = Path(__file__).resolve().parent.parent


def _leer_yaml(nombre: str) -> dict:
    ruta = RAIZ / "config" / nombre
    try:
        return yaml.safe_load(ruta.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        logger.error(f"No existe {ruta}")
        return {}


@lru_cache
def negocio() -> dict:
    return _leer_yaml("business.yaml")


@lru_cache
def prompts() -> dict:
    return _leer_yaml("prompts.yaml")


def reglas() -> dict:
    base = {
        "dias_antes": 1,
        "dias_despues": 4,
        "dias_espera_banco": 3,
        "monto_maximo_auto": 3_000_000,
        "pausa_humano_horas": 12,
        "pago_varias_facturas": True,
        "horas_sin_respuesta": 6,
        "max_consultas_ia_hora": 30,
    }
    base.update(negocio().get("reglas") or {})
    return base


def cuentas_destino() -> list[str]:
    """Ultimos 4 digitos de las cuentas de HUNO que reciben pagos (ignora 'COMPLETAR')."""
    finales = []
    for c in negocio().get("cuentas_destino") or []:
        digitos = re.sub(r"\D", "", str(c))
        if len(digitos) >= 4:
            finales.append(digitos[-4:])
    return finales


def rut_propio() -> str | None:
    """RUT de HUNO (titular de la cuenta): nunca es el RUT de un cliente."""
    from agent.utils import normalizar_rut

    return normalizar_rut(_valor(negocio(), "datos_transferencia.rut"))


def _valor(datos: dict, ruta: str) -> str:
    actual = datos
    for parte in ruta.split("."):
        if not isinstance(actual, dict):
            return ""
        actual = actual.get(parte)
    if isinstance(actual, list):
        return "\n".join(f"- {x}" for x in actual)
    return "" if actual is None else str(actual)


def _formatear_monto(n) -> str:
    try:
        return "$" + f"{int(n):,}".replace(",", ".")
    except (TypeError, ValueError):
        return ""


def _derivados(datos: dict) -> dict:
    """Valores calculados que el prompt usa como {{calculado.*}}."""
    url = str(datos.get("cotizador_url") or "").strip()
    desde = _formatear_monto(datos.get("precio_desde"))
    if url:
        cotizacion = (f"El cliente cotiza su plan en {url}: indica qué necesita y recibe el valor mensual "
                      "en su correo. Entrega ese enlace.")
    else:
        cotizacion = ("El cotizador en línea aún no está disponible. Ofrece que un ejecutivo le prepare la "
                      "cotización y, si acepta, usa escalar_a_persona con motivo \"cotizacion\" y en el resumen "
                      "anota qué servicios le interesan, la empresa y un correo si lo dio.")
    return {"calculado": {"precio_desde": desde, "cotizacion": cotizacion}}


def _knowledge() -> str:
    carpeta = RAIZ / "knowledge"
    partes = []
    if carpeta.is_dir():
        for ruta in sorted(carpeta.glob("*.md")) + sorted(carpeta.glob("*.txt")):
            texto = ruta.read_text(encoding="utf-8")
            texto = re.sub(r"<!--.*?-->", "", texto, flags=re.S).strip()
            if texto:
                partes.append(texto)
    return "\n\n".join(partes)


@lru_cache
def system_prompt() -> str:
    plantilla = prompts().get("system_prompt", "")
    datos = {**negocio(), **_derivados(negocio())}
    texto = re.sub(r"\{\{\s*([\w.]+)\s*\}\}", lambda m: _valor(datos, m.group(1)), plantilla)
    return (texto.rstrip() + "\n\n" + _knowledge()).strip()


# Respuestas que pueden faltar a proposito (el codigo revisa si existen)
_OPCIONALES = {"saludo_conocido", "confirmado_varias", "servicios_cotizador", "recordar_empresa", "recordar_rut"}
_RESPALDO = "Recibí su mensaje. Un ejecutivo de USP le responderá {cuando}."


def respuesta(clave: str, **campos) -> str:
    plantilla = (prompts().get("respuestas") or {}).get(clave, "") or ""
    if not plantilla.strip() and clave not in _OPCIONALES:
        # Si alguien borra una respuesta de prompts.yaml, el cliente igual recibe algo
        logger.warning(f"Falta la respuesta '{clave}' en config/prompts.yaml; se usa una generica")
        plantilla = _RESPALDO
    campos.setdefault("horario", _valor(negocio(), "negocio.horario"))
    if "{cuando}" in plantilla and "cuando" not in campos:
        from agent import horario

        campos["cuando"] = horario.cuando_responde()
    if "{" in plantilla:
        datos = {**negocio(), **_derivados(negocio())}
        for clave_dato in re.findall(r"\{\{\s*([\w.]+)\s*\}\}", plantilla):
            plantilla = plantilla.replace("{{" + clave_dato + "}}", _valor(datos, clave_dato).replace("{", "(").replace("}", ")"))
    try:
        return plantilla.format(**campos)
    except (KeyError, IndexError):
        return plantilla


def recargar():
    """Para que el panel o las pruebas lean cambios sin reiniciar."""
    negocio.cache_clear()
    prompts.cache_clear()
    system_prompt.cache_clear()
