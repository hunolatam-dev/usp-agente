# agent/horario.py — Horario de atención de USP y "cuándo le van a responder"
"""
Se lee de config/business.yaml (atencion). Sirve para que los mensajes de derivación digan
algo concreto ("hoy a partir de las 9:00 hrs", "el lunes 19 a partir de las 9:00 hrs") en
vez de prometer una respuesta inmediata un sábado a las 23:00.
"""

from datetime import date, datetime, time, timedelta, timezone

from agent import config

DIAS = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]
NOMBRES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_POR_DEFECTO = {0: "09:00-18:00", 1: "09:00-18:00", 2: "09:00-18:00", 3: "09:00-18:00", 4: "09:00-17:30"}


def ahora_chile() -> datetime:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/Santiago"))
    except Exception:  # noqa: BLE001 — sin tzdata
        return datetime.now(timezone(timedelta(hours=-3)))


def _hora(texto: str) -> time:
    h, m = str(texto).strip().split(":")
    return time(int(h), int(m))


def bloques() -> dict[int, tuple[time, time]]:
    conf = config.negocio().get("atencion") or {}
    salida = {}
    for i, dia in enumerate(DIAS):
        valor = conf.get(dia) if conf else _POR_DEFECTO.get(i)
        if not valor or str(valor).lower() in ("cerrado", "no", "false"):
            continue
        try:
            desde, hasta = str(valor).split("-")
            salida[i] = (_hora(desde), _hora(hasta))
        except ValueError:
            continue
    return salida


def feriados() -> set[date]:
    salida = set()
    for f in (config.negocio().get("atencion") or {}).get("feriados") or []:
        try:
            salida.add(f if isinstance(f, date) else date.fromisoformat(str(f).strip()))
        except ValueError:
            continue
    return salida


def _bloque_del_dia(d: date) -> tuple[time, time] | None:
    if d in feriados():
        return None
    return bloques().get(d.weekday())


def abierto(momento: datetime | None = None) -> bool:
    momento = momento or ahora_chile()
    b = _bloque_del_dia(momento.date())
    return bool(b and b[0] <= momento.time().replace(tzinfo=None) < b[1])


def proxima_apertura(momento: datetime | None = None) -> datetime | None:
    """Inicio del próximo bloque de atención (o `momento` si ya está abierto)."""
    momento = momento or ahora_chile()
    if abierto(momento):
        return momento
    for dias in range(0, 15):
        d = momento.date() + timedelta(days=dias)
        b = _bloque_del_dia(d)
        if not b:
            continue
        inicio = datetime.combine(d, b[0], tzinfo=momento.tzinfo)
        if inicio > momento:
            return inicio
    return None


def _hhmm(t: time) -> str:
    return f"{t.hour}:{t.minute:02d}"


def cuando_responde(momento: datetime | None = None) -> str:
    """Frase para completar "un ejecutivo le responderá ___"."""
    momento = momento or ahora_chile()
    if abierto(momento):
        return "hoy, dentro del horario de atención"
    nxt = proxima_apertura(momento)
    if nxt is None:
        return "en horario hábil"
    hora = _hhmm(nxt.time())
    dias = (nxt.date() - momento.date()).days
    if dias == 0:
        return f"hoy a partir de las {hora} hrs"
    if dias == 1:
        return f"mañana {NOMBRES[nxt.weekday()]} a partir de las {hora} hrs"
    return f"el {NOMBRES[nxt.weekday()]} {nxt.day} a partir de las {hora} hrs"


def contexto(momento: datetime | None = None) -> str:
    """Una línea para la IA: qué hora es y cuándo puede responder una persona."""
    momento = momento or ahora_chile()
    estado = "DENTRO" if abierto(momento) else "FUERA"
    return (f"Ahora en Chile: {NOMBRES[momento.weekday()]} {momento:%d-%m-%Y %H:%M}, {estado} del horario de "
            f"atención. Si derivas, un ejecutivo responde {cuando_responde(momento)}.")
