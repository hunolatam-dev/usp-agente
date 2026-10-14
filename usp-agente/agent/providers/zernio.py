# agent/providers/zernio.py — Adaptador Zernio (WhatsApp Cloud API con Coexistence)
"""
Contrato tomado del SDK oficial @zernio/node (tipos WebhookPayloadMessage y
WebhookPayloadMessageSent) y de https://docs.zernio.com/platforms/whatsapp

- message.received: lo que escribe el cliente. Los adjuntos de WhatsApp traen una url a
  GET /v1/whatsapp/media/{mediaId} que exige "Authorization: Bearer <API key>" y que
  Meta borra despues de un tiempo: hay que descargarlos apenas llega el webhook.
- message.sent: mensajes que salen del numero. sentVia == "api" son los de este agente;
  cualquier otro (la app del celular, el inbox de Zernio) es un ejecutivo de USP.
"""

import hashlib
import hmac
import logging
import os
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import Request

from agent.providers.base import Adjunto, MensajeEntrante, ProveedorWhatsApp

logger = logging.getLogger("usp")

BASE_URL_POR_DEFECTO = "https://zernio.com/api/v1"
TAMANO_MAXIMO = 15 * 1024 * 1024


class ProveedorZernio(ProveedorWhatsApp):
    def __init__(self):
        self.api_key = os.getenv("ZERNIO_API_KEY", "")
        self.webhook_secret = os.getenv("ZERNIO_WEBHOOK_SECRET", "")
        self.account_id = os.getenv("ZERNIO_ACCOUNT_ID", "")
        self.base_url = (os.getenv("ZERNIO_BASE_URL") or BASE_URL_POR_DEFECTO).rstrip("/")
        self.produccion = os.getenv("ENVIRONMENT") == "production"

        if not self.api_key:
            logger.warning("Falta ZERNIO_API_KEY: el agente no podra responder")
        if not self.webhook_secret:
            if self.produccion:
                logger.error("Falta ZERNIO_WEBHOOK_SECRET: en produccion se RECHAZAN todos los webhooks")
            else:
                logger.warning("Falta ZERNIO_WEBHOOK_SECRET: webhooks sin verificar (solo desarrollo)")

    # ── Recibir ──────────────────────────────────────────────────────────

    async def verificar_firma(self, request: Request, cuerpo: bytes) -> bool:
        if not self.webhook_secret:
            # A diferencia de AgentKit original: en produccion sin secreto NO se acepta nada,
            # porque cualquiera que conozca la URL podria inyectar "comprobantes".
            return not self.produccion

        recibida = request.headers.get("X-Zernio-Signature") or request.headers.get("X-Late-Signature")
        if not recibida:
            logger.warning("Webhook sin firma: rechazado")
            return False
        esperada = hmac.new(self.webhook_secret.encode(), cuerpo, hashlib.sha256).hexdigest()
        try:
            return hmac.compare_digest(esperada, recibida.strip().lower())
        except TypeError:
            return False

    async def parsear_webhook(self, cuerpo: dict) -> list[MensajeEntrante]:
        evento = cuerpo.get("event")
        if evento not in ("message.received", "message.sent"):
            return []
        if cuerpo.get("test"):
            # "Send test" del panel de Zernio: se confirma que llega, pero no se responde
            logger.info(f"Webhook de prueba de Zernio recibido OK ({evento})")
            return []

        m = cuerpo.get("message") or {}
        if m.get("platform") != "whatsapp":
            return []

        cuenta = cuerpo.get("account") or {}
        conversacion = cuerpo.get("conversation") or {}
        contexto = {
            "evento_id": cuerpo.get("id", ""),
            "conversation_id": m.get("conversationId") or conversacion.get("id", ""),
            "account_id": cuenta.get("accountId") or cuenta.get("id", ""),
        }

        if evento == "message.sent":
            via = m.get("sentVia")
            tipo = "saliente_api" if via in ("api", "bulk-api") else "saliente_humano"
            # El telefono del cliente en un mensaje saliente es el participante de la conversacion
            telefono = (conversacion.get("participantId") or "").lstrip("+")
            return [
                MensajeEntrante(
                    telefono=telefono,
                    texto=m.get("text") or "",
                    mensaje_id=m.get("platformMessageId") or m.get("id") or "",
                    tipo=tipo,
                    contexto=contexto,
                )
            ]

        if m.get("direction") != "incoming":
            return []

        remitente = m.get("sender") or {}
        telefono = (remitente.get("phoneNumber") or "").lstrip("+")
        if not telefono:
            # Usuarios con username de WhatsApp pueden no exponer su numero (BSUID, 2026)
            telefono = remitente.get("businessScopedUserId") or remitente.get("id") or ""

        adjuntos = []
        for a in m.get("attachments") or []:
            if not a.get("url"):
                continue
            adjuntos.append(
                Adjunto(
                    tipo=(a.get("type") or "file").lower(),
                    url=a["url"],
                    mime=a.get("mimeType"),
                    es_nota_de_voz=bool((a.get("payload") or {}).get("voice")),
                )
            )

        return [
            MensajeEntrante(
                telefono=telefono,
                texto=m.get("text") or "",
                mensaje_id=m.get("platformMessageId") or m.get("id") or "",
                tipo="entrante",
                nombre=remitente.get("name") or conversacion.get("participantName"),
                adjuntos=adjuntos,
                contexto=contexto,
            )
        ]

    async def descargar_adjunto(self, adjunto: Adjunto) -> tuple[bytes, str] | None:
        url = urljoin(self.base_url + "/", adjunto.url)
        host = (urlparse(url).hostname or "").lower()
        headers = {}
        # La API key SOLO se manda a Zernio, nunca a otro dominio
        if host == "zernio.com" or host.endswith(".zernio.com"):
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as cliente:
                r = await cliente.get(url, headers=headers)
        except httpx.HTTPError as e:
            logger.error(f"No se pudo descargar el adjunto: {e}")
            return None
        if r.status_code != 200:
            logger.error(f"Descarga de adjunto rechazada [{r.status_code}]: {r.text[:200]}")
            return None
        if len(r.content) > TAMANO_MAXIMO:
            logger.warning(f"Adjunto demasiado grande ({len(r.content)} bytes)")
            return None
        mime = (r.headers.get("content-type") or adjunto.mime or "application/octet-stream").split(";")[0]
        return r.content, mime.strip().lower()

    # ── Enviar ───────────────────────────────────────────────────────────

    async def enviar_mensaje(self, telefono: str, mensaje: str, contexto: dict | None = None) -> bool:
        contexto = contexto or {}
        conversation_id = contexto.get("conversation_id")
        account_id = contexto.get("account_id") or self.account_id
        if not self.api_key or not conversation_id or not account_id:
            logger.error("No se puede enviar: falta API key, conversation_id o account_id")
            return False

        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        clave = contexto.get("idempotencia") or contexto.get("evento_id")
        if clave:
            headers["Idempotency-Key"] = f"usp-{clave}"[:200]
        try:
            async with httpx.AsyncClient(timeout=30.0) as cliente:
                r = await cliente.post(
                    f"{self.base_url}/inbox/conversations/{conversation_id}/messages",
                    json={"accountId": account_id, "message": mensaje},
                    headers=headers,
                )
        except httpx.HTTPError as e:
            logger.error(f"Error de red con Zernio: {e}")
            return False
        if 200 <= r.status_code < 300:
            return True
        logger.error(f"Zernio rechazo el envio [{r.status_code}]: {r.text[:300]}")
        return False

    async def verificar_conexion(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "Falta ZERNIO_API_KEY"
        if not self.account_id:
            return True, "ZERNIO_ACCOUNT_ID vacio: se omite el chequeo del numero"
        try:
            async with httpx.AsyncClient(timeout=15.0) as cliente:
                r = await cliente.get(
                    f"{self.base_url}/whatsapp/number-info",
                    params={"accountId": self.account_id},
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
        except httpx.HTTPError as e:
            return False, f"No se pudo contactar a Zernio: {e}"
        if r.status_code != 200:
            return False, f"Zernio respondio {r.status_code}"
        tel = (r.json() or {}).get("phone") or {}
        return True, f"Numero {tel.get('display_phone_number', '?')} (calidad {tel.get('quality_rating', '?')})"
