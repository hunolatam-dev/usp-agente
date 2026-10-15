# Agente USP (HUNO®) — instrucciones para Claude Code

Agente de WhatsApp de cobranzas de USP. Python 3.12, FastAPI, SQLAlchemy async, Zernio, Claude.
Derivado de whatsapp-agentkit. El onboarding original está en `docs/agentkit-original/` y NO se usa:
no ejecutar `/build-agent`, porque sobrescribiría este agente.

## Reglas del dominio (no romper)
- La IA nunca confirma pagos. `comprobantes.py` solo extrae datos; la decisión está en `conciliacion.py` y es determinística.
- Clientes, teléfonos y facturas son de app.huno.cl (`clientes.telefonos`, `facturas`, `facturas_abonos`), leídos en `agent/huno.py`. No cambiar la estructura de esas tablas. Solo un teléfono que esté en la ficha del cliente puede ver sus facturas. Un RUT escrito en el chat sirve para asociar comprobantes, nunca para ver deudas.
- Pago total → `facturas.pagado = true`; pago parcial (solo manual) → fila en `facturas_abonos`. Siempre dentro de la misma transacción que descuenta `usp_movimientos.disponible`.
- La plata de un movimiento se descuenta con un UPDATE condicional sobre `usp_movimientos.disponible`. Cualquier nuevo camino que aplique pagos debe hacer lo mismo.
- La API key de Zernio solo se envía a `*.zernio.com`.
- Todo mensaje entrante recibe respuesta (salvo chat en pausa por ejecutivo). Si algo falla, se envía `respuestas.error` y se abre caso `error_tecnico`. Un caso nuevo de `escalamientos.MOTIVOS` también va en `USP_MOTIVOS` de app.huno.cl.
- En producción, un webhook sin firma válida se rechaza.
- Los montos son CLP enteros. Los RUT se guardan como `12345678-K`. Los teléfonos, solo dígitos con 56.

## Mapa
- `agent/main.py`: webhook, cola en segundo plano, pausa por ejecutivo.
- `agent/flujo.py`: qué hacer con cada mensaje (adjuntos → comprobante; texto → cerebro).
- `agent/cerebro.py`: reglas fijas sin tokens y ciclo de herramientas con Claude.
- `agent/herramientas.py`: herramientas del modelo (facturas, comprobantes, RUT, escalar).
- `agent/conciliacion.py`: reglas de confirmación.
- `agent/huno.py`: lectura de clientes/teléfonos/facturas de app.huno.cl y registro de pagos.
- `agent/importadores.py`: cartola BCI (CSV/XLSX).
- `agent/horario.py`: horario de atención y feriados → "cuándo responde un ejecutivo" (`{cuando}` en prompts.yaml).
- `agent/tareas.py`: revisión cada 30 min (esperando banco, cliente sin responder, avisos pendientes).
- `agent/registro.py`: el cliente registra su número (RUT + correo registrado → nombre → código del Estado de cuenta vía Edge Function `send-otp` y `ec_verificar`). Solo así un número nuevo entra a `clientes.telefonos`.
- `agent/respaldo.py` + `php/usp_respaldo.php`: archivos 180 días; respaldo por correo a alertas@huno.cl al día 170 (vía huno.cl/api porque Railway bloquea SMTP). Nunca borrar un archivo sin `respaldado_en`.
- `agent/admin.py` + `agent/static/panel.html`: panel `/admin` (token `ADMIN_TOKEN`).
- `config/business.yaml`, `config/prompts.yaml`, `knowledge/*.md`: textos y reglas editables.
- `sql/supabase_usp.sql`: esquema de producción. Si se cambia `db.py`, regenerar y probar contra Postgres. Una columna nueva en una tabla existente va también en `_COLUMNAS_NUEVAS` de `db.py` (create_all no altera tablas).

## Antes de entregar cambios
`python -m pytest` debe pasar (las pruebas simulan las tablas de app.huno.cl). Si se toca SQL, correr también con `TEST_DATABASE_URL` apuntando a Postgres.
