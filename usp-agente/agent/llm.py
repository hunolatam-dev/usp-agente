# agent/llm.py — Cliente de Claude compartido
import logging
import os

from anthropic import AsyncAnthropic

logger = logging.getLogger("usp")

# Haiku por defecto: es lo mas barato y alcanza para conversar y leer comprobantes,
# porque la decision de confirmar un pago la toma el codigo (conciliacion.py), no el modelo.
MODELO_CHAT = os.getenv("ANTHROPIC_MODEL") or "claude-haiku-5-5"
MODELO_COMPROBANTES = os.getenv("ANTHROPIC_MODEL_COMPROBANTES") or MODELO_CHAT
ESFUERZO = (os.getenv("ANTHROPIC_EFFORT") or "low").strip()
MAX_TOKENS = int(os.getenv("ANTHROPIC_MAX_TOKENS") or "2048")

_cliente: AsyncAnthropic | None = None
_soporta_esfuerzo = True


def cliente() -> AsyncAnthropic:
    global _cliente
    if _cliente is None:
        clave = (os.getenv("ANTHROPIC_API_KEY") or "").strip().strip("'\"")
        # Las claves de usuario (sk-ant-usr-...) no estan ligadas a un workspace y exigen indicarlo
        espacio = (os.getenv("ANTHROPIC_WORKSPACE_ID") or "").strip()
        extra = {"default_headers": {"anthropic-workspace-id": espacio}} if espacio else {}
        # Sin limite, una llamada colgada deja al cliente esperando minutos: se corta y se deriva
        _cliente = AsyncAnthropic(api_key=clave, timeout=float(os.getenv("ANTHROPIC_TIMEOUT") or 60),
                                  max_retries=2, **extra)
    return _cliente


def _es_error_de_esfuerzo(e: Exception) -> bool:
    return getattr(e, "status_code", None) == 400 and any(
        p in str(e).lower() for p in ("output_config", "effort")
    )


async def crear_mensaje(**kwargs):
    """messages.create con esfuerzo bajo; si el modelo no lo acepta, reintenta sin el."""
    global _soporta_esfuerzo
    kwargs.setdefault("max_tokens", MAX_TOKENS)
    if _soporta_esfuerzo and ESFUERZO:
        try:
            r = await cliente().messages.create(output_config={"effort": ESFUERZO}, **kwargs)
            _log_uso(kwargs.get("model"), r)
            return r
        except Exception as e:  # noqa: BLE001
            if not _es_error_de_esfuerzo(e):
                raise
            logger.warning("El modelo no acepta output_config.effort; se desactiva")
            _soporta_esfuerzo = False
    r = await cliente().messages.create(**kwargs)
    _log_uso(kwargs.get("model"), r)
    return r


def _log_uso(modelo, r):
    u = getattr(r, "usage", None)
    if u is not None:
        logger.info(
            f"Claude {modelo}: {u.input_tokens} in / {u.output_tokens} out"
            f" (cache leido {getattr(u, 'cache_read_input_tokens', 0) or 0})"
        )
