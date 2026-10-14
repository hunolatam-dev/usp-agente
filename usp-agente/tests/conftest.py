import os
import sys
import tempfile
from pathlib import Path

# Base de datos temporal y configuracion de prueba ANTES de importar el agente
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = os.getenv("TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{_tmp}/test.db"
os.environ["ENVIRONMENT"] = "test"
os.environ["ADMIN_TOKEN"] = "token-de-prueba-123456"
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("TELEGRAM_BOT_TOKEN", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from sqlalchemy import delete  # noqa: E402

from agent import config  # noqa: E402
from agent.db import Base, async_session, inicializar_db  # noqa: E402
from agent.providers.base import Adjunto, ProveedorWhatsApp  # noqa: E402

# Datos de transferencia de prueba: cuenta de HUNO terminada en 4321
config.negocio()  # carga cache
config.negocio.cache_info()
_neg = config.negocio()
_neg["cuentas_destino"] = ["12-345-4321"]


class ProveedorFalso(ProveedorWhatsApp):
    def __init__(self):
        self.enviados: list[tuple[str, str, dict]] = []
        self.archivos: dict[str, tuple[bytes, str]] = {}
        self.falla_envio = False

    async def parsear_webhook(self, cuerpo):
        from agent.providers.zernio import ProveedorZernio

        return await ProveedorZernio.parsear_webhook(self, cuerpo)

    async def enviar_mensaje(self, telefono, mensaje, contexto=None):
        if self.falla_envio:
            return False
        self.enviados.append((telefono, mensaje, contexto or {}))
        return True

    async def descargar_adjunto(self, adjunto: Adjunto):
        return self.archivos.get(adjunto.url)


@pytest.fixture
async def db():
    await inicializar_db()
    async with async_session() as s:
        for tabla in reversed(Base.metadata.sorted_tables):
            await s.execute(delete(tabla))
        await s.commit()
    yield


@pytest.fixture
def proveedor():
    return ProveedorFalso()
