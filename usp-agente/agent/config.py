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


def _valor(datos: dict, ruta: str) -> str:
    actual = datos
    for parte in ruta.split("."):
        if not isinstance(actual, dict):
            return ""
        actual = actual.get(parte)
    return "" if actual is None else str(actual)


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
    datos = negocio()
    texto = re.sub(r"\{\{\s*([\w.]+)\s*\}\}", lambda m: _valor(datos, m.group(1)), plantilla)
    return (texto.rstrip() + "\n\n" + _knowledge()).strip()


def respuesta(clave: str, **campos) -> str:
    plantilla = (prompts().get("respuestas") or {}).get(clave, "")
    campos.setdefault("horario", _valor(negocio(), "negocio.horario"))
    try:
        return plantilla.format(**campos)
    except (KeyError, IndexError):
        return plantilla


def recargar():
    """Para que el panel o las pruebas lean cambios sin reiniciar."""
    negocio.cache_clear()
    prompts.cache_clear()
    system_prompt.cache_clear()
