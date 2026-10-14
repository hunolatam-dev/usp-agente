# Agente USP (HUNO®) — instrucciones para Claude Code

Agente de WhatsApp de cobranzas de USP. Python 3.12, FastAPI, SQLAlchemy async, Zernio, Claude.
Derivado de whatsapp-agentkit. El onboarding original está en `docs/agentkit-original/` y NO se usa:
no ejecutar `/build-agent`, porque sobrescribiría este agente.

## Reglas del dominio (no romper)
- La IA nunca confirma pagos. `comprobantes.py` solo extrae datos; la decisión está en `conciliacion.py` y es determinística.
- Solo un teléfono en `usp_contactos` puede ver facturas de un RUT. Un RUT escrito en el chat sirve para asociar comprobantes, nunca para ver deudas.
- La plata de un movimiento se descuenta con un UPDATE condicional sobre `usp_movimientos.disponible`. Cualquier nuevo camino que aplique pagos debe hacer lo mismo.
- La API key de Zernio solo se envía a `*.zernio.com`.
- En producción, un webhook sin firma válida se rechaza.
- Los montos son CLP enteros. Los RUT se guardan como `12345678-K`. Los teléfonos, solo dígitos con 56.

## Mapa
- `agent/main.py`: webhook, cola en segundo plano, pausa por ejecutivo.
- `agent/flujo.py`: qué hacer con cada mensaje (adjuntos → comprobante; texto → cerebro).
- `agent/cerebro.py`: reglas fijas sin tokens y ciclo de herramientas con Claude.
- `agent/herramientas.py`: herramientas del modelo (facturas, comprobantes, RUT, escalar).
- `agent/conciliacion.py`: reglas de confirmación.
- `agent/importadores.py`: cartola BCI, facturas Facto y contactos (CSV/XLSX).
- `agent/admin.py` + `agent/static/panel.html`: panel `/admin` (token `ADMIN_TOKEN`).
- `config/business.yaml`, `config/prompts.yaml`, `knowledge/*.md`: textos y reglas editables.
- `sql/supabase_usp.sql`: esquema de producción. Si se cambia `db.py`, regenerar y probar contra Postgres.

## Antes de entregar cambios
`python -m pytest` debe pasar. Si se toca SQL, correr también con `TEST_DATABASE_URL` apuntando a Postgres.
