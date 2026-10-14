# agent/providers/base.py — Interfaz comun de proveedores de WhatsApp
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from fastapi import Request


@dataclass
class Adjunto:
    tipo: str            # image | file | audio | video | sticker | ...
    url: str
    mime: str | None = None
    es_nota_de_voz: bool = False


@dataclass
class MensajeEntrante:
    telefono: str                 # solo digitos, sin "+"
    texto: str
    mensaje_id: str
    # entrante        -> lo escribio el cliente
    # saliente_humano -> lo escribio un ejecutivo desde la app del celular (Coexistence)
    # saliente_api    -> lo mando este agente (se ignora)
    tipo: str = "entrante"
    nombre: str | None = None
    adjuntos: list[Adjunto] = field(default_factory=list)
    contexto: dict = field(default_factory=dict)
    # contexto: evento_id, conversation_id, account_id


class ProveedorWhatsApp(ABC):
    @abstractmethod
    async def parsear_webhook(self, cuerpo: dict) -> list[MensajeEntrante]: ...

    @abstractmethod
    async def enviar_mensaje(self, telefono: str, mensaje: str, contexto: dict | None = None) -> bool: ...

    @abstractmethod
    async def descargar_adjunto(self, adjunto: Adjunto) -> tuple[bytes, str] | None:
        """Devuelve (bytes, mime) o None si no se pudo."""

    async def verificar_firma(self, request: Request, cuerpo: bytes) -> bool:
        return True

    async def verificar_conexion(self) -> tuple[bool, str]:
        return True, "sin chequeo"
