import os

from agent.providers.base import Adjunto, MensajeEntrante, ProveedorWhatsApp


def obtener_proveedor() -> ProveedorWhatsApp:
    nombre = (os.getenv("WHATSAPP_PROVIDER") or "zernio").strip().lower()
    if nombre == "zernio":
        from agent.providers.zernio import ProveedorZernio

        return ProveedorZernio()
    raise ValueError(f"Proveedor no soportado: '{nombre}'. Este agente usa: zernio")


__all__ = ["Adjunto", "MensajeEntrante", "ProveedorWhatsApp", "obtener_proveedor"]
