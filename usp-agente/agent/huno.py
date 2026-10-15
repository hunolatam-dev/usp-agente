# agent/huno.py — Clientes y facturas de app.huno.cl (misma base Supabase)
"""
El agente NO tiene su propia lista de clientes ni de facturas: lee las de app.huno.cl.

- Telefono -> cliente: columna clientes.telefonos (uno o varios numeros, cada uno con el
  nombre de su dueno). El formato de ese texto lo define app.huno.cl, asi que aqui se
  extraen los numeros de forma tolerante (JSON o texto libre).
- Saldo de una factura = facturas.monto - suma(facturas_abonos.monto), si no esta pagada.
- Al confirmar un pago: pago total -> facturas.pagado = true; pago parcial (solo desde el
  panel) -> nueva fila en facturas_abonos.
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, or_, select, update

from agent.db import AbonoHuno, ClienteHuno, FacturaHuno, ahora, async_session, engine
from agent.utils import normalizar_rut, normalizar_telefono

logger = logging.getLogger("usp")

USUARIO_AGENTE = "Agente WhatsApp USP"
_TTL = 120  # segundos que se guarda en memoria la lista de clientes

_RE_NUMERO = re.compile(r"\+?\d[\d\s().-]{6,18}\d")


@dataclass
class Cliente:
    id: int
    rut: str | None              # normalizado: 76123456-7
    nombre: str                  # razon social o nombre visible
    telefonos: list[tuple[str, str | None]] = field(default_factory=list)  # (56912345678, "Maria")

    def nombre_de(self, telefono: str) -> str | None:
        return next((n for t, n in self.telefonos if t == telefono), None)


def _telefono_valido(digitos: str | None) -> str | None:
    tel = normalizar_telefono(digitos)
    # Chile: 56 + 9 digitos. Otros paises: se aceptan entre 10 y 15 digitos.
    if tel and (len(tel) == 11 and tel.startswith("56") or (not tel.startswith("56") and 10 <= len(tel) <= 15)):
        return tel
    return None


_PALABRAS_NO_NOMBRE = re.compile(
    r"\b(tel[eé]fonos?|fono|cel(ular)?|whatsapp|wsp|m[oó]vil|n[uú]mero|valor|value|nombre|name|tipo|"
    r"etiqueta|label|principal|contacto|fijo|oficina|personal|trabajo|null|none|true|false)\b", re.I)
_RE_NOMBRE_VALIDO = re.compile(r"^[A-Za-zÁÉÍÓÚÜÑáéíóúüñ'][A-Za-zÁÉÍÓÚÜÑáéíóúüñ' .-]{1,79}$")


def _limpiar_nombre(texto: str | None) -> str | None:
    """Deja solo algo que parezca un nombre de persona; si no, None."""
    t = re.sub(r"[\d+()\-:|/,;\[\]{}\"'=_]+", " ", str(texto or ""))
    t = _PALABRAS_NO_NOMBRE.sub(" ", t)
    t = " ".join(t.split()).strip(" .")
    return t if t and _RE_NOMBRE_VALIDO.match(t) else None


def parsear_telefonos(texto: str | None) -> list[tuple[str, str | None]]:
    """Extrae (telefono, nombre del dueno) de clientes.telefonos, sea JSON o texto libre."""
    if not texto or not str(texto).strip():
        return []
    salida: list[tuple[str, str | None]] = []

    def agregar(numero, nombre):
        tel = _telefono_valido(re.sub(r"\D", "", str(numero or "")))
        if tel and tel not in [t for t, _ in salida]:
            salida.append((tel, _limpiar_nombre(nombre)))

    datos = None
    try:
        datos = json.loads(texto)
        if isinstance(datos, str):  # JSON guardado dos veces como texto
            datos = json.loads(datos)
    except (ValueError, TypeError):
        pass

    if isinstance(datos, (list, dict)):
        elementos = datos if isinstance(datos, list) else [datos]
        for e in elementos:
            if isinstance(e, dict):
                # El numero: cualquier valor que tenga forma de telefono (la clave puede ser
                # "valor", "telefono", "numero"...). El nombre: la clave que suene a nombre o,
                # si no hay, otro texto del mismo elemento que parezca nombre de persona.
                textos = {str(k): str(v) for k, v in e.items() if v not in (None, "")}
                numeros = [v for v in textos.values() if _RE_NUMERO.search(v) and _telefono_valido(re.sub(r"\D", "", v))]
                nombre = next((_limpiar_nombre(v) for k, v in textos.items()
                               if re.search(r"nombre|name|due|persona|quien|contacto|etiqueta|label|desc", k, re.I)
                               and _limpiar_nombre(v)), None)
                if nombre is None:
                    nombre = next((_limpiar_nombre(v) for v in textos.values() if v not in numeros and _limpiar_nombre(v)), None)
                for n in numeros:
                    agregar(n, nombre)
            elif isinstance(e, (str, int)):
                for m in _RE_NUMERO.finditer(str(e)):
                    agregar(m.group(0), _limpiar_nombre(str(e).replace(m.group(0), " ")))
        return salida

    # Texto libre: se separa por renglones / ; / , y en cada trozo el resto del texto es el nombre
    for trozo in re.split(r"[\n;|]+|,(?!\d)", str(texto)):
        for m in _RE_NUMERO.finditer(trozo):
            agregar(m.group(0), _limpiar_nombre(trozo.replace(m.group(0), " ")))
    return salida


# ── Cache de clientes ────────────────────────────────────────────────────

_cache: dict = {"t": None, "clientes": []}


def invalidar_cache():
    _cache["t"] = None


async def clientes(forzar: bool = False) -> list[Cliente]:
    # Ojo: time.monotonic() puede ser menor que el TTL recien arrancado el contenedor,
    # por eso "sin cache" es None y no 0.
    if not forzar and _cache["t"] is not None and time.monotonic() - _cache["t"] < _TTL:
        return _cache["clientes"]
    async with async_session() as s:
        filas = (await s.execute(select(ClienteHuno))).scalars().all()
    lista = [
        Cliente(
            id=f.id,
            rut=normalizar_rut(f.rut),
            nombre=(f.nombre_empresa or f.nombre or f.rut or f"Cliente {f.id}").strip(),
            telefonos=parsear_telefonos(f.telefonos),
        )
        for f in filas
    ]
    _cache.update(t=time.monotonic(), clientes=lista)
    return lista


async def clientes_de_telefono(telefono: str) -> list[Cliente]:
    return [c for c in await clientes() if any(t == telefono for t, _ in c.telefonos)]


async def clientes_de_rut(rut: str | None) -> list[Cliente]:
    if not rut:
        return []
    return [c for c in await clientes() if c.rut == rut]


async def cliente_por_id(cid: int) -> Cliente | None:
    return next((c for c in await clientes() if c.id == cid), None)


def elegir(texto: str, opciones: list[Cliente]) -> Cliente | None:
    """
    Interpreta la respuesta a "¿de cual empresa?": numero de opcion ("2", "la 2"),
    RUT, o parte del nombre ("elqui"). Devuelve None si no queda claro.
    """
    from agent.utils import buscar_ruts, sin_tildes

    t = sin_tildes(texto or "").strip()
    numeros = re.findall(r"\b(\d{1,2})\b", t)
    if len(numeros) == 1 and len(t) <= 25 and 1 <= int(numeros[0]) <= len(opciones):
        return opciones[int(numeros[0]) - 1]
    for rut in buscar_ruts(texto):
        hit = [c for c in opciones if c.rut == rut]
        if len(hit) == 1:
            return hit[0]
    palabras = [w for w in re.findall(r"[a-z0-9]{3,}", t)
                if w not in {"empresa", "la", "el", "de", "del", "spa", "ltda", "limitada", "eirl", "sociedad", "comercial", "consultar", "quiero"}]
    if palabras:
        hit = [c for c in opciones if any(w in sin_tildes(c.nombre) for w in palabras)]
        if len(hit) == 1:
            return hit[0]
    return None


def lista_opciones(opciones: list[Cliente]) -> str:
    from agent.utils import formatear_rut

    return "\n".join(f"{i}. {c.nombre}" + (f" ({formatear_rut(c.rut)})" if c.rut else "")
                     for i, c in enumerate(opciones, 1))


# ── Facturas ─────────────────────────────────────────────────────────────


@dataclass
class FacturaPendiente:
    id: int
    cliente_id: int
    folio: str
    monto: int
    abonado: int
    saldo: int
    fecha_emision: date | None
    anio: int | None
    mes: int | None
    glosa: str | None

    @property
    def vence(self) -> date | None:
        """Plazo de pago: N dias corridos desde la emision (config/business.yaml: plazo_pago_dias)."""
        from datetime import timedelta

        from agent import config

        if not self.fecha_emision:
            return None
        return self.fecha_emision + timedelta(days=int(config.negocio().get("plazo_pago_dias") or 15))

    @property
    def periodo(self) -> str | None:
        return f"{self.mes:02d}/{self.anio}" if self.mes and self.anio else None


def _folio(f: FacturaHuno) -> str:
    return (f.folio_sii or f.folio or str(f.id)).strip()


async def facturas_pendientes(s, cliente_ids: list[int]) -> list[FacturaPendiente]:
    if not cliente_ids:
        return []
    abonos = (
        select(AbonoHuno.factura_id, func.coalesce(func.sum(AbonoHuno.monto), 0).label("abonado"))
        .group_by(AbonoHuno.factura_id)
        .subquery()
    )
    filas = (
        await s.execute(
            select(FacturaHuno, func.coalesce(abonos.c.abonado, 0))
            .outerjoin(abonos, abonos.c.factura_id == FacturaHuno.id)
            .where(FacturaHuno.cliente_id.in_(cliente_ids),
                   or_(FacturaHuno.pagado.is_(None), FacturaHuno.pagado.is_(False)))
            .order_by(FacturaHuno.fecha_emision.is_(None), FacturaHuno.fecha_emision,
                      FacturaHuno.anio, FacturaHuno.mes, FacturaHuno.id)
        )
    ).all()
    salida = []
    for f, abonado in filas:
        monto = int(f.monto or 0)
        saldo = monto - int(abonado or 0)
        if saldo <= 0:
            continue
        salida.append(FacturaPendiente(f.id, f.cliente_id, _folio(f), monto, int(abonado or 0), saldo,
                                       f.fecha_emision, f.anio, f.mes, f.glosa))
    return salida


async def saldo_bloqueado(s, factura_id: int) -> tuple[FacturaHuno | None, int]:
    """Lee la factura bloqueando la fila (Postgres) y devuelve (factura, saldo)."""
    f = (
        await s.execute(select(FacturaHuno).where(FacturaHuno.id == factura_id).with_for_update())
    ).scalars().first()
    if f is None:
        return None, 0
    abonado = (
        await s.execute(select(func.coalesce(func.sum(AbonoHuno.monto), 0)).where(AbonoHuno.factura_id == factura_id))
    ).scalar_one()
    return f, int(f.monto or 0) - int(abonado or 0)


async def aplicar_pago(s, factura_id: int, monto: int, fecha_pago: date, usuario: str) -> tuple[bool, int]:
    """
    Registra el pago en app.huno.cl dentro de la transaccion `s` (no hace commit).
    - Si cubre el saldo completo: pagado = true.
    - Si es menor: agrega un abono (y si con eso llega a 0, tambien pagado = true).
    Devuelve (ok, saldo_restante). ok=False si la factura ya esta pagada o el monto excede el saldo.
    """
    f, saldo = await saldo_bloqueado(s, factura_id)
    if f is None or f.pagado or monto <= 0 or monto > saldo:
        return False, max(saldo, 0)
    if monto < saldo:
        s.add(AbonoHuno(factura_id=factura_id, monto=monto))
        await s.execute(
            update(FacturaHuno).where(FacturaHuno.id == factura_id)
            .values(usuario_ultima_accion=usuario, fecha_ultima_accion=ahora())
        )
        return True, saldo - monto
    r = await s.execute(
        update(FacturaHuno)
        .where(FacturaHuno.id == factura_id, or_(FacturaHuno.pagado.is_(None), FacturaHuno.pagado.is_(False)))
        .values(pagado=True, fecha_pago=fecha_pago, usuario_pago=usuario,
                usuario_ultima_accion=usuario, fecha_ultima_accion=ahora())
    )
    return r.rowcount == 1, 0


async def factura(s, factura_id: int) -> FacturaHuno | None:
    return await s.get(FacturaHuno, factura_id)


def folio_de(f: FacturaHuno | None) -> str | None:
    return _folio(f) if f is not None else None


# ── Registro de un telefono por WhatsApp ─────────────────────────────────

_RE_CORREO = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _lista_json(texto) -> list | None:
    """clientes.telefonos / clientes.correos: app.huno.cl guarda JSON.stringify([{valor, label}])."""
    if texto is None or str(texto).strip() == "":
        return []
    try:
        datos = json.loads(texto)
        if isinstance(datos, str):
            datos = json.loads(datos)
    except (ValueError, TypeError):
        return None
    return datos if isinstance(datos, list) else None


async def correos_de(cliente_id: int) -> set[str]:
    """Correos registrados del cliente (clientes.correos y, si existe, clientes.portal_email), en minusculas."""
    async with async_session() as s:
        fila = await s.get(ClienteHuno, cliente_id)
        texto = fila.correos if fila else None
        portal = None
        if engine.dialect.name == "postgresql":
            from sqlalchemy import text as sql

            portal = (await s.execute(sql("select to_jsonb(c)->>'portal_email' from public.clientes c where c.id = :i"),
                                      {"i": cliente_id})).scalar()
    salida = {m.group(0).lower() for m in _RE_CORREO.finditer(str(texto or ""))}
    if portal:
        salida |= {m.group(0).lower() for m in _RE_CORREO.finditer(portal)}
    return salida


async def agregar_telefono(cliente_id: int, telefono: str, nombre: str) -> bool:
    """
    Agrega el numero a clientes.telefonos con el mismo formato que usa app.huno.cl
    ([{"valor": "+569...", "label": "Nombre"}]). Devuelve False si ya estaba.
    """
    async with async_session() as s:
        fila = (await s.execute(select(ClienteHuno).where(ClienteHuno.id == cliente_id).with_for_update())).scalars().first()
        if fila is None:
            raise ValueError(f"Cliente {cliente_id} no existe")
        if telefono in [t for t, _ in parsear_telefonos(fila.telefonos)]:
            return False
        lista = _lista_json(fila.telefonos)
        if lista is None:
            # Texto antiguo que no es JSON: se pasa al formato de la app sin perder los numeros
            lista = [{"valor": "+" + t, "label": n or ""} for t, n in parsear_telefonos(fila.telefonos)]
        lista.append({"valor": "+" + telefono, "label": nombre})
        fila.telefonos = json.dumps(lista, ensure_ascii=False)
        await s.commit()
    invalidar_cache()
    return True
