# agent/herramientas.py — Herramientas que Claude puede usar en la conversacion
"""
Ninguna herramienta confirma pagos. Las de lectura solo muestran datos de los RUT
asociados al telefono que escribe (tabla usp_contactos).
"""

import json

from sqlalchemy import select

from agent import conciliacion, escalamientos, memoria
from agent.db import Cliente, Comprobante, Factura, async_session
from agent.utils import formatear_monto, formatear_rut, normalizar_rut

DEFINICIONES = [
    {
        "name": "consultar_facturas_pendientes",
        "description": "Lista las facturas pendientes de pago de las empresas asociadas al telefono del cliente. "
        "Usala cuando pregunten cuanto deben, que facturas tienen pendientes o montos a pagar.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "estado_comprobantes",
        "description": "Estado de los ultimos comprobantes de pago enviados desde este telefono.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "registrar_rut_empresa",
        "description": "Guarda el RUT de la empresa que el cliente dice representar, para asociar sus comprobantes. "
        "No da acceso a ver deudas.",
        "input_schema": {
            "type": "object",
            "properties": {"rut": {"type": "string", "description": "RUT con digito verificador, ej 76.123.456-7"}},
            "required": ["rut"],
        },
    },
    {
        "name": "escalar_a_persona",
        "description": "Deriva la conversacion a un ejecutivo de USP. Usala cuando el cliente lo pida, cuando no "
        "puedas resolver con tus herramientas o informacion, ante reclamos, o si el telefono no esta registrado.",
        "input_schema": {
            "type": "object",
            "properties": {
                "motivo": {"type": "string", "enum": ["pedido_cliente", "registro_contacto", "consulta", "reclamo"]},
                "resumen": {"type": "string", "description": "Resumen breve del caso para el ejecutivo"},
            },
            "required": ["motivo", "resumen"],
        },
    },
]

ETIQUETAS = {
    "confirmado": "pago confirmado",
    "pendiente_banco": "recibido, esperando que se refleje en el banco",
    "esperando_rut": "recibido, falta el RUT de la empresa",
    "escalado": "en revision por un ejecutivo de USP",
    "no_legible": "no se pudo leer; hay que reenviarlo",
    "duplicado": "reenvio de un comprobante anterior",
    "recibido": "recibido, en proceso",
}


async def consultar_facturas_pendientes(telefono: str) -> dict:
    async with async_session() as s:
        ruts = await conciliacion.ruts_de_telefono(s, telefono)
        if not ruts:
            return {"resultado": "telefono_no_registrado"}
        empresas = []
        for rut in ruts:
            cli = await s.get(Cliente, rut)
            facturas = (
                await s.execute(
                    select(Factura).where(Factura.rut == rut, Factura.estado == "pendiente")
                    .order_by(Factura.fecha_vencimiento, Factura.id)
                )
            ).scalars().all()
            empresas.append({
                "empresa": cli.razon_social if cli else rut,
                "rut": formatear_rut(rut),
                "total_pendiente": formatear_monto(sum(f.saldo for f in facturas)),
                "facturas": [
                    {"folio": f.folio, "saldo": formatear_monto(f.saldo),
                     "emision": f.fecha_emision.isoformat() if f.fecha_emision else None,
                     "vencimiento": f.fecha_vencimiento.isoformat() if f.fecha_vencimiento else None}
                    for f in facturas[:15]
                ],
            })
        return {"resultado": "ok", "empresas": empresas,
                "nota": "Datos segun la ultima carga. Pagos de hoy pueden no estar reflejados."}


async def estado_comprobantes(telefono: str) -> dict:
    async with async_session() as s:
        comps = (
            await s.execute(
                select(Comprobante).where(Comprobante.telefono == telefono, Comprobante.estado != "duplicado")
                .order_by(Comprobante.id.desc()).limit(5)
            )
        ).scalars().all()
        salida = []
        for c in comps:
            item = {"enviado": c.creado_en.date().isoformat() if c.creado_en else None,
                    "monto": formatear_monto(c.monto) if c.monto else None,
                    "estado": ETIQUETAS.get(c.estado, c.estado)}
            if c.estado == "confirmado" and c.factura_id:
                fac = await s.get(Factura, c.factura_id)
                item["factura"] = fac.folio if fac else None
            salida.append(item)
    return {"comprobantes": salida} if salida else {"comprobantes": [], "nota": "No hay comprobantes recibidos desde este telefono"}


async def registrar_rut_empresa(telefono: str, rut: str) -> dict:
    rut_ok = normalizar_rut(rut)
    if not rut_ok:
        return {"resultado": "rut_invalido"}
    await memoria.guardar_rut_declarado(telefono, rut_ok)
    resultados = await conciliacion.reevaluar_pendientes(telefono, estados=("esperando_rut",))
    return {
        "resultado": "rut_registrado",
        "rut": formatear_rut(rut_ok),
        "comprobantes_reevaluados": [r.mensaje() for _, r in resultados],
        "instruccion": "Si hay comprobantes reevaluados, transmite esos mensajes tal cual al cliente.",
    }


async def escalar_a_persona(telefono: str, motivo: str, resumen: str) -> dict:
    esc = await escalamientos.crear(telefono, motivo, resumen)
    return {"resultado": "derivado", "caso": esc.id}


async def ejecutar(nombre: str, entrada: dict, telefono: str) -> str:
    try:
        if nombre == "consultar_facturas_pendientes":
            r = await consultar_facturas_pendientes(telefono)
        elif nombre == "estado_comprobantes":
            r = await estado_comprobantes(telefono)
        elif nombre == "registrar_rut_empresa":
            r = await registrar_rut_empresa(telefono, str(entrada.get("rut", "")))
        elif nombre == "escalar_a_persona":
            motivo = entrada.get("motivo") if entrada.get("motivo") in escalamientos.MOTIVOS else "consulta"
            r = await escalar_a_persona(telefono, motivo, str(entrada.get("resumen", ""))[:2000])
        else:
            r = {"error": f"herramienta desconocida {nombre}"}
    except Exception as e:  # noqa: BLE001
        r = {"error": "fallo interno", "detalle": str(e)[:200]}
    return json.dumps(r, ensure_ascii=False)
