# Agente de WhatsApp de USP — HUNO®

Atiende el WhatsApp de cobranzas de USP (+56 9 7870 1038): responde dudas, recibe comprobantes de transferencia, los cruza con la cartola de BCI y confirma el pago de la factura. Lo que no calza exacto lo deriva a una persona.

Basado en [WhatsApp AgentKit](https://github.com/Hainrixz/whatsapp-agentkit) (MIT). Usa Zernio con Coexistence, así que el número sigue funcionando en la app del celular.

## Cómo decide

**Todo mensaje recibe respuesta.** Si algo falla (la IA, la base, WhatsApp), el cliente recibe un aviso y el caso queda abierto para una persona.

| El cliente… | El agente… |
|---|---|
| Saluda, agradece, pide datos de transferencia, horario o servicios, o pide hablar con una persona | Responde con un texto fijo, **sin llamar a la IA** |
| Pregunta cuánto debe | Muestra sus facturas de Finanzas (app.huno.cl), pero solo si su teléfono está en la ficha del cliente. Si no debe nada, dice que está **al día** y ofrece el detalle de sus pagos. Con una sola empresa responde sin IA. Si el número no está registrado, le ofrece registrarlo (ver abajo) |
| Pide el detalle de sus pagos | Últimas facturas pagadas (con fecha de pago) y abonos de las pendientes |
| Escribe desde un número que no está en ninguna ficha | Le dice que no se pudo validar el número y le ofrece registrarlo con el RUT de la empresa y el correo registrado |
| Pregunta por planes o precios | Indica los 5 servicios y que los valores comienzan en $55.000 mensuales. Mientras no exista el cotizador (`cotizador_url`), ofrece que un ejecutivo cotice |
| Envía un comprobante (imagen o PDF) | La IA lee monto, fecha, N° de operación y cuenta de destino. **El código** decide si el pago se confirma |
| Envía otro documento (factura, contrato, Excel, Word…) | Lo guarda y lo deriva a un ejecutivo: puede ser para contabilidad o RRHH |
| Escribe varios mensajes seguidos | Si el primero parece incompleto ("hola"), espera hasta 3 s y responde una sola vez |
| Escribe mientras atiende un ejecutivo | No responde (sí avisa si se confirma un pago) |
| Escribe desde el celular de USP o el inbox de Zernio (ejecutivo) | El agente se calla 12 h en ese chat. Las respuestas automáticas de WhatsApp Business (bienvenida/ausencia) **no** lo callan |

Cuando deriva, el mensaje dice **cuándo** responderá un ejecutivo según el horario (`atencion` en `config/business.yaml`, con feriados): "hoy, dentro del horario", "mañana martes a partir de las 9:00 hrs", "el lunes 19 a partir de las 9:00 hrs".

**Un pago se confirma solo si se cumple todo esto:**
1. El comprobante es legible y no se usó antes (ni el mismo archivo ni el mismo N° de operación).
2. El monto es ≤ $3.000.000 (configurable).
3. La cuenta de destino es de HUNO.
4. Se sabe qué cliente paga: por el teléfono de su ficha en app.huno.cl, por el RUT que escribe el cliente o por el RUT de origen.
5. Hay una factura pendiente de ese cliente en Finanzas de app.huno.cl por **el saldo exacto** (monto menos abonos). Si hay varias, se paga la más antigua. Si ninguna calza sola pero el monto es **la suma exacta de sus facturas pendientes más antiguas**, se pagan todas esas (`pago_varias_facturas`).
6. Hay **una** transferencia en la cartola por ese monto, en las fechas del comprobante y sin usar.

Si solo falta que aparezca en el banco, el comprobante queda "esperando banco" y se confirma solo al cargar la cartola, con aviso por WhatsApp. Todo lo demás (pagos parciales, montos que no calzan, cuenta distinta, comprobante repetido) va al panel como **caso**.

**Revisión cada 30 minutos** (también desde el panel: `POST /admin/api/revisar`):
- Reintenta los comprobantes "esperando banco"; si pasan `dias_espera_banco` sin aparecer, abre un caso.
- Si el cliente no indicó el RUT o la empresa de su comprobante en `horas_sin_respuesta`, abre un caso. Si después responde y se confirma, el caso se cierra solo.
- Avisa los pagos confirmados que no se pudieron avisar, apenas el cliente vuelve a escribir.

Al confirmar, la factura queda **pagada en Finanzas de app.huno.cl** (`facturas.pagado`, con fecha de pago y usuario "Agente WhatsApp USP"). Un pago parcial confirmado a mano desde el panel queda como **abono** (`facturas_abonos`).

La base de datos impide usar la misma transferencia dos veces, incluso si llegan dos comprobantes al mismo tiempo desde chats distintos.

**Registro del número por el propio cliente** (`agent/registro.py`, sin gastar tokens):
1. El cliente escribe el RUT de la empresa y el correo registrado. Se cruzan con la ficha en app.huno.cl (`clientes.rut` y `clientes.correos` / `portal_email`).
2. Si calzan, se le pide su nombre.
3. Se le envía **el mismo código del Estado de cuenta de huno.cl** (Edge Function `send-otp`, que lo manda al correo registrado de la empresa). Lo escribe en el chat y se valida con `ec_verificar` (5 intentos, vence en 10 minutos). Si no llega, escribe «nuevo código».
4. Con el código correcto, el número queda en `clientes.telefonos` con el nombre de quien habla (mismo formato que la app) y en los casos resueltos de app.huno.cl queda la constancia. Desde ahí se le saluda por su nombre y se le muestran las facturas de esa empresa.

Límites por número en 24 h: 3 intentos con RUT/correo que no calzan y 3 envíos de código; al superarlos se deriva a un ejecutivo. Un comprobante que esperaba el RUT se confirma al terminar el registro.

**Plazo de pago:** 15 días corridos desde la emisión de la factura (`plazo_pago_dias`). app.huno.cl usa la misma regla en la alerta naranja de Finanzas y en el Reporte de cobranza.

**Archivos recibidos** (comprobantes y documentos): se conservan **180 días corridos**. Al **día 170** se envían por correo a **alertas@huno.cl** como respaldo y al día 180 se borra el archivo (los datos leídos del comprobante se mantienen). Nunca se borra un archivo sin respaldo: si el correo falla, se reintenta cada día y se abre un caso. Como Railway (plan Hobby) bloquea el correo SMTP, el envío pasa por `php/usp_respaldo.php`, que se sube a `huno.cl/api/` y solo puede enviar a alertas@huno.cl. Se configura en `archivos` de `config/business.yaml` y con `RESPALDO_TOKEN` en Railway. Desde el panel: `POST /admin/api/respaldo/prueba` envía un correo de prueba.

## Puesta en marcha

### 1. Supabase
1. En el SQL Editor del proyecto, ejecuta `sql/supabase_usp.sql`. Crea las tablas `usp_*` con RLS activado: la anon key de las webs no puede leerlas.
2. En *Project Settings → Database → Connection string*, copia la conexión del **Session pooler**. Esa cadena va en `DATABASE_URL`.

### 2. Zernio
1. Crea la cuenta en zernio.com y conecta el número **+56 9 7870 1038 con Coexistence** (escaneando el QR desde WhatsApp Business del celular de USP).
2. En *Settings → API Keys*, crea una key. Esa key va en `ZERNIO_API_KEY`.
3. **Abre la app WhatsApp Business del celular de USP al menos una vez cada 14 días.** Meta lo exige para mantener la conexión de Coexistence. El celular no necesita estar encendido para que el agente responda: todo corre en Railway, Supabase y Zernio.

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
- Los **clientes, teléfonos y facturas se leen directo de app.huno.cl**: no hay que cargarlos. Para que el agente reconozca un número, debe estar en el campo *teléfonos* de la ficha del cliente.
- Entra al panel en `https://TU-APP.up.railway.app/admin` con el `ADMIN_TOKEN` y carga la **Cartola BCI Pyme** (Excel o CSV), idealmente cada mañana y cada tarde. En la pestaña *Clientes y teléfonos* puedes revisar qué números reconoce.

En `ejemplos/` hay una cartola de muestra con el formato que reconoce.

## Uso diario del panel `/admin`
- **Casos abiertos:** ver el comprobante, confirmar a mano (eligiendo factura y movimiento; un pago de varias facturas se registra una por una), pausar o reactivar el agente en ese chat, y cerrar el caso.
- **Cargar cartola:** al subirla se reevalúan los comprobantes en espera y se avisa a los clientes.
- **Confirmados sin avisar:** WhatsApp solo permite escribir texto libre hasta 24 h después del último mensaje del cliente. Los que quedaron fuera se avisan desde el celular.

Para recibir los casos nuevos en Telegram, configura `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`.

## Rapidez
- Las respuestas fijas (saludo, datos de transferencia, deuda y pagos con una empresa, registro del número) no usan la IA: salen en 1 a 2 segundos.
- Mientras prepara la respuesta, el cliente ve "escribiendo..." (y su mensaje queda leído).
- Solo espera a que el cliente termine de escribir cuando el mensaje parece incompleto ("hola", "una consulta"): hasta 3 s (`AGRUPAR_SEGUNDOS`). Una pregunta completa se responde al tiro.
- En Railway, el servicio debe estar en la región **US West (California)**, la más cercana a la base de Supabase (us-west-2, Oregon), y sin "Serverless" (que lo apaga cuando no hay mensajes). Cada respuesta hace varias consultas a la base: con regiones lejanas se suman segundos.
- En los logs de Railway cada mensaje muestra `respondido en X s` y cada llamada a la IA `Claude …: N in / M out … en X s`.

## Costos
- **Zernio:** $0 por 1–2 números conectados. Meta cobra los mensajes según su tarifa vigente para Chile.
- **Claude:** Haiku por defecto. Los saludos y las respuestas de comprobantes son textos fijos y no gastan tokens. El prompt va en caché.
- **Railway:** plan Hobby.

## Desarrollo
```bash
pip install -r requirements-dev.txt
python -m pytest            # 116 pruebas (SQLite)
TEST_DATABASE_URL=postgresql://... python -m pytest   # contra Postgres
uvicorn agent.main:app --reload --port 8000
```

## Próximas fases (pendientes)
- **Banco automático:** reemplazar la carga manual de la cartola por una fuente automática de BCI. Solo hay que llamar a `importadores` o insertar en `usp_movimientos` con `fuente="api"`.
- **Comprobante de pago:** que Finanzas de app.huno.cl genere y envíe el comprobante (hoy se marca la factura pagada, pero el comprobante PDF lo envía el equipo).
- **Recordatorios de cobro:** requieren plantillas aprobadas por Meta (Zernio permite enviarlas).
