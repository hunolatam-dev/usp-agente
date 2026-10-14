-- Esquema del agente USP (WhatsApp de cobranzas HUNO®) para Supabase
-- Ejecutar UNA vez en Supabase -> SQL Editor. Es idempotente (se puede repetir).
-- Las tablas usan prefijo usp_ para no chocar con las de app.huno.cl.
--
-- Seguridad: RLS activado SIN politicas. Asi, la anon key que usan las paginas web
-- (app.huno.cl, huno.cl) NO puede leer ni escribir estas tablas. El agente se conecta
-- directo a Postgres con la cadena de conexion (rol postgres), que no pasa por RLS.

begin;

CREATE TABLE IF NOT EXISTS usp_clientes (
	rut VARCHAR(12) NOT NULL, 
	razon_social VARCHAR(300) NOT NULL, 
	email VARCHAR(200), 
	actualizado_en TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (rut)
);

ALTER TABLE usp_clientes ENABLE ROW LEVEL SECURITY;

CREATE TABLE IF NOT EXISTS usp_conversaciones (
	telefono VARCHAR(50) NOT NULL, 
	conversation_id VARCHAR(100), 
	account_id VARCHAR(100), 
	nombre_whatsapp VARCHAR(200), 
	ultimo_entrante_en TIMESTAMP WITH TIME ZONE, 
	pausado_hasta TIMESTAMP WITH TIME ZONE, 
	rut_declarado VARCHAR(12), 
	PRIMARY KEY (telefono)
);

ALTER TABLE usp_conversaciones ENABLE ROW LEVEL SECURITY;

CREATE TABLE IF NOT EXISTS usp_eventos_procesados (
	evento_id VARCHAR(200) NOT NULL, 
	creado_en TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (evento_id)
);

CREATE INDEX IF NOT EXISTS ix_usp_eventos_procesados_creado_en ON usp_eventos_procesados (creado_en);
ALTER TABLE usp_eventos_procesados ENABLE ROW LEVEL SECURITY;

CREATE TABLE IF NOT EXISTS usp_mensajes (
	id SERIAL NOT NULL, 
	telefono VARCHAR(50) NOT NULL, 
	role VARCHAR(20) NOT NULL, 
	content TEXT NOT NULL, 
	creado_en TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_usp_mensajes_telefono ON usp_mensajes (telefono);
ALTER TABLE usp_mensajes ENABLE ROW LEVEL SECURITY;

CREATE TABLE IF NOT EXISTS usp_movimientos (
	id SERIAL NOT NULL, 
	fecha DATE NOT NULL, 
	monto BIGINT NOT NULL, 
	disponible BIGINT NOT NULL, 
	descripcion TEXT NOT NULL, 
	documento VARCHAR(60), 
	rut_origen VARCHAR(12), 
	huella VARCHAR(64) NOT NULL, 
	fuente VARCHAR(30) NOT NULL, 
	creado_en TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (huella)
);

CREATE INDEX IF NOT EXISTS ix_usp_movimientos_disponible ON usp_movimientos (disponible);
CREATE INDEX IF NOT EXISTS ix_usp_movimientos_fecha ON usp_movimientos (fecha);
CREATE INDEX IF NOT EXISTS ix_usp_movimientos_monto ON usp_movimientos (monto);
ALTER TABLE usp_movimientos ENABLE ROW LEVEL SECURITY;

CREATE TABLE IF NOT EXISTS usp_contactos (
	id SERIAL NOT NULL, 
	telefono VARCHAR(50) NOT NULL, 
	rut VARCHAR(12) NOT NULL, 
	nombre VARCHAR(200), 
	creado_en TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_usp_contacto UNIQUE (telefono, rut), 
	FOREIGN KEY(rut) REFERENCES usp_clientes (rut)
);

CREATE INDEX IF NOT EXISTS ix_usp_contactos_rut ON usp_contactos (rut);
CREATE INDEX IF NOT EXISTS ix_usp_contactos_telefono ON usp_contactos (telefono);
ALTER TABLE usp_contactos ENABLE ROW LEVEL SECURITY;

CREATE TABLE IF NOT EXISTS usp_facturas (
	id SERIAL NOT NULL, 
	tipo_dte INTEGER NOT NULL, 
	folio VARCHAR(30) NOT NULL, 
	rut VARCHAR(12) NOT NULL, 
	monto_total BIGINT NOT NULL, 
	saldo BIGINT NOT NULL, 
	fecha_emision DATE, 
	fecha_vencimiento DATE, 
	estado VARCHAR(20) NOT NULL, 
	origen VARCHAR(30) NOT NULL, 
	actualizado_en TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_usp_factura UNIQUE (tipo_dte, folio), 
	FOREIGN KEY(rut) REFERENCES usp_clientes (rut)
);

CREATE INDEX IF NOT EXISTS ix_usp_facturas_estado ON usp_facturas (estado);
CREATE INDEX IF NOT EXISTS ix_usp_facturas_rut ON usp_facturas (rut);
ALTER TABLE usp_facturas ENABLE ROW LEVEL SECURITY;

CREATE TABLE IF NOT EXISTS usp_comprobantes (
	id SERIAL NOT NULL, 
	telefono VARCHAR(50) NOT NULL, 
	conversation_id VARCHAR(100), 
	account_id VARCHAR(100), 
	mensaje_id VARCHAR(200), 
	archivo BYTEA, 
	mime VARCHAR(100), 
	sha256 VARCHAR(64), 
	es_comprobante BOOLEAN, 
	monto BIGINT, 
	fecha DATE, 
	numero_operacion VARCHAR(80), 
	banco_origen VARCHAR(100), 
	rut_origen VARCHAR(12), 
	nombre_origen VARCHAR(200), 
	cuenta_destino VARCHAR(60), 
	rut_destino VARCHAR(12), 
	datos_crudos TEXT, 
	estado VARCHAR(30) NOT NULL, 
	motivo TEXT, 
	factura_id INTEGER, 
	movimiento_id INTEGER, 
	notificado BOOLEAN NOT NULL, 
	creado_en TIMESTAMP WITH TIME ZONE NOT NULL, 
	actualizado_en TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(factura_id) REFERENCES usp_facturas (id), 
	FOREIGN KEY(movimiento_id) REFERENCES usp_movimientos (id)
);

CREATE INDEX IF NOT EXISTS ix_usp_comprobantes_creado_en ON usp_comprobantes (creado_en);
CREATE INDEX IF NOT EXISTS ix_usp_comprobantes_estado ON usp_comprobantes (estado);
CREATE INDEX IF NOT EXISTS ix_usp_comprobantes_numero_operacion ON usp_comprobantes (numero_operacion);
CREATE INDEX IF NOT EXISTS ix_usp_comprobantes_sha256 ON usp_comprobantes (sha256);
CREATE INDEX IF NOT EXISTS ix_usp_comprobantes_telefono ON usp_comprobantes (telefono);
ALTER TABLE usp_comprobantes ENABLE ROW LEVEL SECURITY;

CREATE TABLE IF NOT EXISTS usp_escalamientos (
	id SERIAL NOT NULL, 
	telefono VARCHAR(50) NOT NULL, 
	motivo VARCHAR(60) NOT NULL, 
	resumen TEXT NOT NULL, 
	comprobante_id INTEGER, 
	estado VARCHAR(20) NOT NULL, 
	creado_en TIMESTAMP WITH TIME ZONE NOT NULL, 
	resuelto_en TIMESTAMP WITH TIME ZONE, 
	resuelto_por VARCHAR(100), 
	nota_resolucion TEXT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(comprobante_id) REFERENCES usp_comprobantes (id)
);

CREATE INDEX IF NOT EXISTS ix_usp_escalamientos_estado ON usp_escalamientos (estado);
CREATE INDEX IF NOT EXISTS ix_usp_escalamientos_telefono ON usp_escalamientos (telefono);
ALTER TABLE usp_escalamientos ENABLE ROW LEVEL SECURITY;

CREATE TABLE IF NOT EXISTS usp_pagos (
	id SERIAL NOT NULL, 
	factura_id INTEGER NOT NULL, 
	movimiento_id INTEGER NOT NULL, 
	comprobante_id INTEGER, 
	monto BIGINT NOT NULL, 
	origen VARCHAR(20) NOT NULL, 
	registrado_por VARCHAR(100), 
	creado_en TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_usp_pago UNIQUE (movimiento_id, factura_id), 
	FOREIGN KEY(factura_id) REFERENCES usp_facturas (id), 
	FOREIGN KEY(movimiento_id) REFERENCES usp_movimientos (id), 
	FOREIGN KEY(comprobante_id) REFERENCES usp_comprobantes (id)
);

CREATE INDEX IF NOT EXISTS ix_usp_pagos_factura_id ON usp_pagos (factura_id);
CREATE INDEX IF NOT EXISTS ix_usp_pagos_movimiento_id ON usp_pagos (movimiento_id);
ALTER TABLE usp_pagos ENABLE ROW LEVEL SECURITY;

-- Revocar acceso de la API REST de Supabase (anon / authenticated) por si acaso
revoke all on table usp_clientes, usp_conversaciones, usp_eventos_procesados, usp_mensajes, usp_movimientos, usp_contactos, usp_facturas, usp_comprobantes, usp_escalamientos, usp_pagos from anon, authenticated;

commit;
