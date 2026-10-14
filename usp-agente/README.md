# Agente de WhatsApp de USP — HUNO®

Atiende el WhatsApp de cobranzas de USP (+56 9 7870 1038): responde dudas, recibe comprobantes de transferencia, los cruza con la cartola de BCI y confirma el pago de la factura. Lo que no calza exacto lo deriva a una persona.

Basado en [WhatsApp AgentKit](https://github.com/Hainrixz/whatsapp-agentkit) (MIT). Usa Zernio con Coexistence, así que el número sigue funcionando en la app del celular.

## Cómo decide

| El cliente… | El agente… |
|---|---|
| Saluda o agradece | Responde con un texto fijo, sin llamar a la IA |
| Pregunta cuánto debe | Muestra sus facturas, pero solo si el teléfono está registrado como contacto de esa empresa |
| Envía un comprobante (imagen o PDF) | La IA lee monto, fecha, N° de operación y cuenta de destino. **El código** decide si el pago se confirma |
| Escribe desde el celular de USP (ejecutivo) | El agente se calla 12 h en ese chat |

**Un pago se confirma solo si se cumple todo esto:**
1. El comprobante es legible y no se usó antes (ni el mismo archivo ni el mismo N° de operación).
2. El monto es ≤ $3.000.000 (configurable).
3. La cuenta de destino es de HUNO.
4. Se sabe qué empresa paga: por el teléfono registrado, por el RUT que escribe el cliente o por el RUT de origen.
5. Hay una factura pendiente de esa empresa por **el monto exacto**. Si hay varias, se paga la más antigua.
6. Hay **una** transferencia en la cartola por ese monto, en las fechas del comprobante y sin usar.

Si solo falta que aparezca en el banco, el comprobante queda "esperando banco" y se confirma solo al cargar la cartola, con aviso por WhatsApp. Todo lo demás (pagos parciales, de varias facturas, montos que no calzan, cuenta distinta, comprobante repetido) va al panel como **caso**.

La base de datos impide usar la misma transferencia dos veces, incluso si llegan dos comprobantes al mismo tiempo desde chats distintos.

## Puesta en marcha

### 1. Supabase
1. En el SQL Editor del proyecto, ejecuta `sql/supabase_usp.sql`. Crea las tablas `usp_*` con RLS activado: la anon key de las webs no puede leerlas.
2. En *Project Settings → Database → Connection string*, copia la conexión del **Session pooler**. Esa cadena va en `DATABASE_URL`.

### 2. Zernio
1. Crea la cuenta en zernio.com y conecta el número **+56 9 7870 1038 con Coexistence** (escaneando el QR desde WhatsApp Business del celular de USP).
2. En *Settings → API Keys*, crea una key. Esa key va en `ZERNIO_API_KEY`.

### 3. Railway
1. Sube esta carpeta a un repo privado (por ejemplo `hunolatam-dev/usp-agente`).
2. En Railway: *New Project → Deploy from GitHub repo*. Detecta el `Dockerfile` solo.
3. En *Variables*, carga las de `.env.example`. Como mínimo: `ANTHROPIC_API_KEY`, `ZERNIO_API_KEY`, `ZERNIO_WEBHOOK_SECRET`, `DATABASE_URL`, `ADMIN_TOKEN` y `ENVIRONMENT=production`.
4. En *Settings → Networking → Generate Domain* obtienes la URL. Al abrirla debe responder `{"status":"ok"...}`.

### 4. Webhook en Zernio
En *Webhooks → Create*:
- **URL:** `https://TU-APP.up.railway.app/webhook`
- **Secret:** el mismo valor de `ZERNIO_WEBHOOK_SECRET`
- **Eventos:** `message.received` y `message.sent`. Este último permite que el agente se calle cuando un ejecutivo responde desde el celular.

Prueba con "Send test": debe responder 200. Los eventos de prueba no se contestan.

### 5. Completar datos y cargar información
- En `config/business.yaml`, reemplaza todo lo que dice **COMPLETAR**: titular, RUT, número de cuenta, correo y `cuentas_destino`.
- En `knowledge/faq_usp.md`, revisa las respuestas frecuentes. El agente no inventa nada que no esté ahí.
- Entra al panel en `https://TU-APP.up.railway.app/admin` con el `ADMIN_TOKEN` y carga:
  1. **Facturas** (export de Facto): folio, RUT, razón social, total, saldo y vencimiento.
  2. **Contactos**: teléfono y RUT de cada cliente.
  3. **Cartola BCI Pyme** (Excel o CSV), idealmente cada mañana y cada tarde.

En `ejemplos/` hay archivos de muestra con el formato que reconoce.

## Uso diario del panel `/admin`
- **Casos abiertos:** ver el comprobante, confirmar a mano (eligiendo factura y movimiento; un pago de varias facturas se registra una por una), pausar o reactivar el agente en ese chat, y cerrar el caso.
- **Cargar cartola:** al subirla se reevalúan los comprobantes en espera y se avisa a los clientes.
- **Confirmados sin avisar:** WhatsApp solo permite escribir texto libre hasta 24 h después del último mensaje del cliente. Los que quedaron fuera se avisan desde el celular.

Para recibir los casos nuevos en Telegram, configura `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`.

## Costos
- **Zernio:** $0 por 1–2 números conectados. Meta cobra los mensajes según su tarifa vigente para Chile.
- **Claude:** Haiku por defecto. Los saludos y las respuestas de comprobantes son textos fijos y no gastan tokens. El prompt va en caché.
- **Railway:** plan Hobby.

## Desarrollo
```bash
pip install -r requirements-dev.txt
python -m pytest            # 38 pruebas (SQLite)
TEST_DATABASE_URL=postgresql://... python -m pytest   # contra Postgres
uvicorn agent.main:app --reload --port 8000
```

## Próximas fases (pendientes)
- **Banco automático:** reemplazar la carga manual de la cartola por una fuente automática de BCI. Solo hay que llamar a `importadores` o insertar en `usp_movimientos` con `fuente="api"`.
- **Finanzas de app.huno.cl en lugar de Facto:** que Finanzas escriba directo en `usp_facturas` (`origen="finanzas"`) y genere el comprobante de pago.
- **Recordatorios de cobro:** requieren plantillas aprobadas por Meta (Zernio permite enviarlas).
