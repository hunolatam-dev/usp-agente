# agent/db.py — Base de datos del agente USP
"""
Todas las tablas del agente viven aqui, con prefijo "usp_" para no chocar con las
tablas de app.huno.cl dentro del mismo Supabase.

- SQLite en local (pruebas), PostgreSQL (Supabase) en produccion.
- El esquema de produccion se crea con sql/supabase_usp.sql (con RLS activado).
  create_all() solo crea lo que falte; no altera tablas existentes.

Montos: siempre en pesos chilenos, enteros (sin decimales).
"""

import logging
import os
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

load_dotenv()
logger = logging.getLogger("usp")


def _normalizar_url(url: str) -> str:
    """SQLAlchemy asincrono necesita el driver explicito. Tolera errores de pegado comunes."""
    url = url.strip().strip("'\"").strip()
    if url.upper().startswith("DATABASE_URL="):
        url = url.split("=", 1)[1].strip().strip("'\"").strip()
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    return url


DATABASE_URL = _normalizar_url(os.getenv("DATABASE_URL") or "sqlite+aiosqlite:///./usp_agente.db")

if DATABASE_URL.startswith("sqlite") and os.getenv("ENVIRONMENT") == "production":
    logger.warning(
        "Produccion con SQLite: en Railway se borra todo en cada redespliegue. "
        "Configura DATABASE_URL con la conexion de Supabase."
    )

_extra = {}
if DATABASE_URL.startswith("postgresql+asyncpg"):
    # El pooler de Supabase (puerto 6543, modo transaction) no soporta prepared
    # statements cacheados: sin esto asyncpg falla de forma intermitente.
    _extra["connect_args"] = {"statement_cache_size": 0}

engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True, **_extra)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def ahora() -> datetime:
    return datetime.now(timezone.utc)


def hoy_chile() -> date:
    """Fecha de hoy en Chile (UTC-3 / UTC-4). Suficiente para comparar fechas de pago."""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/Santiago")).date()
    except Exception:  # noqa: BLE001 — sin tzdata en el contenedor
        return (datetime.now(timezone.utc) - timedelta(hours=3)).date()


def como_utc(dt: datetime | None) -> datetime | None:
    """SQLite devuelve fechas sin zona; las tratamos como UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    pass


# ── Conversacion ─────────────────────────────────────────────────────────


class Mensaje(Base):
    """Historial de conversacion por telefono (solo texto)."""

    __tablename__ = "usp_mensajes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telefono: Mapped[str] = mapped_column(String(50), index=True)
    role: Mapped[str] = mapped_column(String(20))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)


class EventoProcesado(Base):
    """Eventos de webhook ya atendidos (Zernio entrega 'al menos una vez')."""

    __tablename__ = "usp_eventos_procesados"

    evento_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora, index=True)


class Conversacion(Base):
    """Estado por telefono: como responderle, si un humano tomo el chat, RUT declarado."""

    __tablename__ = "usp_conversaciones"

    telefono: Mapped[str] = mapped_column(String(50), primary_key=True)
    conversation_id: Mapped[str | None] = mapped_column(String(100))
    account_id: Mapped[str | None] = mapped_column(String(100))
    nombre_whatsapp: Mapped[str | None] = mapped_column(String(200))
    ultimo_entrante_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Si un ejecutivo responde desde el celular, el agente se calla hasta esta hora
    pausado_hasta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # RUT que el cliente escribio en el chat (NO da acceso a ver deudas)
    rut_declarado: Mapped[str | None] = mapped_column(String(12))


# ── Datos de app.huno.cl (tablas que YA existen en Supabase) ─────────────
# Solo se declaran las columnas que usa el agente. Viven en otra "metadata" para que
# create_all() NUNCA intente crearlas ni tocarlas en produccion; las pruebas las crean aparte.


class BaseHuno(DeclarativeBase):
    pass


class ClienteHuno(BaseHuno):
    __tablename__ = "clientes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rut: Mapped[str | None] = mapped_column(Text)
    nombre: Mapped[str | None] = mapped_column(Text)
    nombre_empresa: Mapped[str | None] = mapped_column(Text)
    telefonos: Mapped[str | None] = mapped_column(Text)
    correos: Mapped[str | None] = mapped_column(Text)
    activo: Mapped[bool | None] = mapped_column(Boolean)


class FacturaHuno(BaseHuno):
    __tablename__ = "facturas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cliente_id: Mapped[int | None] = mapped_column(Integer, index=True)
    folio: Mapped[str | None] = mapped_column(Text)
    folio_sii: Mapped[str | None] = mapped_column(Text)
    monto: Mapped[int | None] = mapped_column(Integer)
    anio: Mapped[int | None] = mapped_column(Integer)
    mes: Mapped[int | None] = mapped_column(Integer)
    pagado: Mapped[bool | None] = mapped_column(Boolean)
    fecha_pago: Mapped[date | None] = mapped_column(Date)
    usuario_pago: Mapped[str | None] = mapped_column(Text)
    fecha_emision: Mapped[date | None] = mapped_column(Date)
    glosa: Mapped[str | None] = mapped_column(Text)
    usuario_ultima_accion: Mapped[str | None] = mapped_column(Text)
    fecha_ultima_accion: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AbonoHuno(BaseHuno):
    __tablename__ = "facturas_abonos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    factura_id: Mapped[int | None] = mapped_column(Integer, index=True)
    monto: Mapped[int | None] = mapped_column(Integer)


# ── Banco ────────────────────────────────────────────────────────────────


class Movimiento(Base):
    """Abonos de la cartola BCI. Solo se guardan los ingresos."""

    __tablename__ = "usp_movimientos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fecha: Mapped[date] = mapped_column(Date, index=True)
    monto: Mapped[int] = mapped_column(BigInteger, index=True)
    # Lo que queda sin aplicar a facturas. Se descuenta con un UPDATE condicional, asi dos
    # comprobantes simultaneos nunca pueden usar la misma plata.
    disponible: Mapped[int] = mapped_column(BigInteger, index=True)
    descripcion: Mapped[str] = mapped_column(Text, default="")
    documento: Mapped[str | None] = mapped_column(String(60))
    rut_origen: Mapped[str | None] = mapped_column(String(12))
    # Huella para que re-importar la misma cartola no duplique movimientos
    huella: Mapped[str] = mapped_column(String(64), unique=True)
    fuente: Mapped[str] = mapped_column(String(30), default="cartola")  # cartola | api
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)


# ── Comprobantes y pagos ─────────────────────────────────────────────────


class Comprobante(Base):
    __tablename__ = "usp_comprobantes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telefono: Mapped[str] = mapped_column(String(50), index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(100))
    account_id: Mapped[str | None] = mapped_column(String(100))
    mensaje_id: Mapped[str | None] = mapped_column(String(200))
    archivo: Mapped[bytes | None] = mapped_column(LargeBinary)
    mime: Mapped[str | None] = mapped_column(String(100))
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)

    # Lo que la IA leyo del comprobante (no decide nada; solo extrae)
    es_comprobante: Mapped[bool | None] = mapped_column(Boolean)
    monto: Mapped[int | None] = mapped_column(BigInteger)
    fecha: Mapped[date | None] = mapped_column(Date)
    numero_operacion: Mapped[str | None] = mapped_column(String(80), index=True)
    banco_origen: Mapped[str | None] = mapped_column(String(100))
    rut_origen: Mapped[str | None] = mapped_column(String(12))
    nombre_origen: Mapped[str | None] = mapped_column(String(200))
    cuenta_destino: Mapped[str | None] = mapped_column(String(60))
    rut_destino: Mapped[str | None] = mapped_column(String(12))
    datos_crudos: Mapped[str | None] = mapped_column(Text)

    estado: Mapped[str] = mapped_column(String(30), default="recibido", index=True)
    # recibido | confirmado | pendiente_banco | esperando_rut | escalado | no_legible
    motivo: Mapped[str | None] = mapped_column(Text)
    factura_id: Mapped[int | None] = mapped_column(Integer)  # facturas.id de app.huno.cl
    movimiento_id: Mapped[int | None] = mapped_column(ForeignKey("usp_movimientos.id"))
    # Si se confirmo despues (al cargar la cartola) y no se pudo avisar por WhatsApp
    notificado: Mapped[bool] = mapped_column(Boolean, default=False)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora, index=True)
    actualizado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)


class Pago(Base):
    """
    Un pago aplicado a una factura. Una transferencia puede repartirse en varias facturas
    (solo a mano, desde el panel); el total nunca supera el monto de la transferencia porque
    se descuenta de usp_movimientos.disponible.
    """

    __tablename__ = "usp_pagos"
    __table_args__ = (UniqueConstraint("movimiento_id", "factura_id", name="uq_usp_pago"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    factura_id: Mapped[int] = mapped_column(Integer, index=True)  # facturas.id de app.huno.cl
    movimiento_id: Mapped[int] = mapped_column(ForeignKey("usp_movimientos.id"), index=True)
    comprobante_id: Mapped[int | None] = mapped_column(ForeignKey("usp_comprobantes.id"))
    monto: Mapped[int] = mapped_column(BigInteger)
    origen: Mapped[str] = mapped_column(String(20), default="agente")  # agente | manual
    registrado_por: Mapped[str | None] = mapped_column(String(100))
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)


class Escalamiento(Base):
    """Casos que el agente deriva a una persona de USP."""

    __tablename__ = "usp_escalamientos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telefono: Mapped[str] = mapped_column(String(50), index=True)
    motivo: Mapped[str] = mapped_column(String(60))
    resumen: Mapped[str] = mapped_column(Text, default="")
    comprobante_id: Mapped[int | None] = mapped_column(ForeignKey("usp_comprobantes.id"))
    estado: Mapped[str] = mapped_column(String(20), default="abierto", index=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)
    resuelto_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resuelto_por: Mapped[str | None] = mapped_column(String(100))
    nota_resolucion: Mapped[str | None] = mapped_column(Text)


async def inicializar_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
