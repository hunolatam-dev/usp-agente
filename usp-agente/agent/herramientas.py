# agent/herramientas.py — Herramientas que Claude puede usar en la conversacion
"""
Ninguna herramienta confirma pagos. Las de lectura solo muestran datos de los clientes
que tienen registrado el telefono que escribe en su ficha de app.huno.cl.
"""

import json

from sqlalchemy import select

from agent import conciliacion, config, escalamientos, huno, memoria
from agent.db import Comprobante, FacturaHuno, async_session, hoy_chile
from agent.utils import formatear_monto, formatear_rut, normalizar_rut

DEFINICIONES = [
    {
        "name": "consultar_facturas_pendientes",
        "description": "Lista las facturas pendientes de pago de una empresa asociada al telefono del cliente. "
        "Usala cuando pregunten cuanto deben, que facturas tienen pendientes o montos a pagar. Si el telefono "
        "tiene varias empresas y no se indica cual, devuelve las opciones para que el cliente elija.",
        "input_schema": {
            "type": "object",
            "properties": {
                "empresa": {
                    "type": "string",
                    "description": "La empresa que eligio el cliente: numero de opcion, RUT o nombre. "
                    "Usa 'todas' solo si pide explicitamente todas sus empresas.",
                }
            },
        },
    },
    {
        "name": "consultar_pagos_realizados",
        "description": "Ultimas facturas PAGADAS de una empresa del cliente (folio, periodo, monto y fecha de pago) y "
        "abonos de las pendientes. Usala cuando pida el detalle o historial de sus pagos. Mismas reglas de empresa que "
        "consultar_facturas_pendientes.",
        "input_schema": {
            "type": "object",
            "properties": {"empresa": {"type": "string", "description": "Numero de opcion, RUT o nombre de la empresa"}},
        },
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
        "name": "registrar_telefono",
        "description": "El cliente quiere registrar este numero de WhatsApp en su empresa y dio el RUT de la empresa "
        "y el correo registrado en HUNO. El sistema valida los datos y sigue el registro (pide su nombre y le envia un "
        "codigo al correo). Transmite tal cual el mensaje que devuelve.",
        "input_schema": {
            "type": "object",
            "properties": {
                "rut": {"type": "string", "description": "RUT de la empresa, ej 76.123.456-7"},
                "correo": {"type": "string", "description": "Correo que dio el cliente"},
            },
            "required": ["rut", "correo"],
        },
    },
    {
        "name": "elegir_empresa_comprobante",
        "description": "Cuando hay un comprobante esperando que el cliente indique a cual de sus empresas corresponde "
        "el pago (aparece en 'Quien escribe') y el cliente responde, pasa su respuesta. El sistema valida el pago; "
        "transmite el mensaje que devuelve tal cual.",
        "input_schema": {
            "type": "object",
            "properties": {"empresa": {"type": "string", "description": "Numero de opcion, RUT o nombre que dio el cliente"}},
            "required": ["empresa"],
        },
    },
    {
        "name": "escalar_a_persona",
        "description": "Deriva la conversacion a un ejecutivo de USP. Usala cuando el cliente lo pida, cuando no "
        "puedas resolver con tus herramientas o informacion, ante reclamos, si el telefono no esta registrado y pide "
        "datos de su cuenta, o si quiere una cotizacion (motivo cotizacion).",
        "input_schema": {
            "type": "object",
            "properties": {
                "motivo": {"type": "string", "enum": ["pedido_cliente", "registro_contacto", "consulta", "reclamo", "cotizacion"]},
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
    "esperando_empresa": "recibido, falta que indique a cual de sus empresas corresponde",
    "escalado": "en revision por un ejecutivo de USP",
    "no_legible": "no se pudo leer; hay que reenviarlo",
    "duplicado": "reenvio de un comprobante anterior",
    "recibido": "recibido, en proceso",
}


async def _clientes_para(telefono: str, empresa: str | None) -> tuple[list[huno.Cliente], dict | None]:
    """Empresas del telefono que se van a mostrar, o la respuesta para la IA si falta algo."""
    clientes = sorted(await huno.clientes_de_telefono(telefono), key=lambda c: c.nombre.lower())
    if not clientes:
        from agent import registro

        return [], {"resultado": "telefono_no_registrado", "mensaje_para_el_cliente": await registro.invitacion(telefono),
                    "instruccion": "No muestres deudas ni pagos. Transmite este mensaje tal cual."}
    if len(clientes) > 1 and (empresa or "").strip().lower() not in ("todas", "todos", "all"):
        elegido = huno.elegir(empresa, clientes) if empresa else None
        if elegido is None:
            return [], {
                "resultado": "elegir_empresa",
                "opciones": huno.lista_opciones(clientes),
                "instruccion": "Este numero esta asociado a varias empresas. Muestra estas opciones tal cual y "
                "pregunta de cual quiere consultar. No muestres facturas todavia."
                + (" La respuesta anterior no coincidio con ninguna opcion." if empresa else ""),
            }
        clientes = [elegido]
    return clientes, None


def _fecha(d) -> str:
    return d.strftime("%d-%m-%Y") if d else "sin fecha"


async def deuda_de(cliente: huno.Cliente, telefono: str | None = None) -> dict:
    """Facturas pendientes de una empresa y el mensaje listo para el cliente."""
    async with async_session() as s:
        facturas = await huno.facturas_pendientes(s, [cliente.id])
        espera = []
        if telefono:
            espera = (await s.execute(
                select(Comprobante).where(Comprobante.telefono == telefono, Comprobante.estado == "pendiente_banco")
            )).scalars().all()
    hoy = hoy_chile()
    if not facturas:
        mensaje = config.respuesta("al_dia", empresa=cliente.nombre)
    else:
        lineas = []
        for f in facturas[:15]:
            vencida = bool(f.vence and f.vence < hoy)
            lineas.append(
                f"- N° {f.folio}" + (f" ({f.periodo})" if f.periodo else "") + f": {formatear_monto(f.saldo)}"
                + (f" (abonado {formatear_monto(f.abonado)})" if f.abonado else "")
                + (f" · vence el {_fecha(f.vence)}" if f.vence else "") + (" · VENCIDA" if vencida else ""))
        if len(facturas) > 15:
            lineas.append(f"- y {len(facturas) - 15} más")
        extras = []
        if any(f.vence and f.vence < hoy for f in facturas):
            extras.append(config.respuesta("aviso_vencidas"))
        for c in espera:
            extras.append(config.respuesta("aviso_comprobante_en_espera", monto=formatear_monto(c.monto or 0)))
        mensaje = config.respuesta(
            "facturas_pendientes", empresa=cliente.nombre, cantidad=len(facturas),
            total=formatear_monto(sum(f.saldo for f in facturas)), lista="\n".join(lineas),
            extras=("\n\n" + "\n\n".join(e for e in extras if e)) if any(extras) else "")
    return {
        "empresa": cliente.nombre,
        "rut": formatear_rut(cliente.rut) if cliente.rut else None,
        "al_dia": not facturas,
        "total_pendiente": formatear_monto(sum(f.saldo for f in facturas)),
        "facturas": [
            {"folio": f.folio, "saldo": formatear_monto(f.saldo), "total_factura": formatear_monto(f.monto),
             "abonado": formatear_monto(f.abonado) if f.abonado else None, "periodo": f.periodo, "glosa": f.glosa,
             "emision": f.fecha_emision.isoformat() if f.fecha_emision else None,
             "vence": f.vence.isoformat() if f.vence else None, "vencida": bool(f.vence and f.vence < hoy)}
            for f in facturas[:15]
        ],
        "mensaje_para_el_cliente": mensaje,
    }


async def consultar_facturas_pendientes(telefono: str, empresa: str | None = None) -> dict:
    clientes, falta = await _clientes_para(telefono, empresa)
    if falta:
        return falta
    empresas = [await deuda_de(c, telefono) for c in clientes]
    return {"resultado": "ok", "empresas": empresas,
            "instruccion": "Transmite mensaje_para_el_cliente tal cual (si hay varias empresas, uno tras otro). "
                           "Si una empresa esta al dia, solo di que esta al dia: no menciones pagos de hoy ni "
                           "comprobantes."}


async def pagos_de(cliente: huno.Cliente, limite: int = 6) -> dict:
    """Ultimas facturas pagadas y abonos de las pendientes, con el mensaje listo para el cliente."""
    async with async_session() as s:
        pagadas = (await s.execute(
            select(FacturaHuno).where(FacturaHuno.cliente_id == cliente.id, FacturaHuno.pagado.is_(True))
            .order_by(FacturaHuno.fecha_pago.is_(None), FacturaHuno.fecha_pago.desc(), FacturaHuno.id.desc())
            .limit(limite)
        )).scalars().all()
        con_abono = [f for f in await huno.facturas_pendientes(s, [cliente.id]) if f.abonado]
    lineas = [f"- N° {huno.folio_de(f)}" + (f" ({f.mes:02d}/{f.anio})" if f.mes and f.anio else "")
              + f": {formatear_monto(f.monto or 0)} · pagada el {_fecha(f.fecha_pago)}" for f in pagadas]
    lineas += [f"- N° {f.folio}" + (f" ({f.periodo})" if f.periodo else "")
               + f": abonado {formatear_monto(f.abonado)} de {formatear_monto(f.monto)} (saldo {formatear_monto(f.saldo)})"
               for f in con_abono]
    if lineas:
        mensaje = config.respuesta("pagos_realizados", empresa=cliente.nombre, lista="\n".join(lineas))
    else:
        mensaje = config.respuesta("sin_pagos", empresa=cliente.nombre)
    return {"empresa": cliente.nombre, "pagos": len(pagadas), "abonos": len(con_abono), "mensaje_para_el_cliente": mensaje}


async def consultar_pagos_realizados(telefono: str, empresa: str | None = None) -> dict:
    clientes, falta = await _clientes_para(telefono, empresa)
    if falta:
        return falta
    return {"resultado": "ok", "empresas": [await pagos_de(c) for c in clientes],
            "instruccion": "Transmite mensaje_para_el_cliente tal cual."}


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
            if c.estado == "confirmado":
                item["factura"] = (await conciliacion.resultado_confirmado(s, c)).folio
            salida.append(item)
    return {"comprobantes": salida} if salida else {"comprobantes": [], "nota": "No hay comprobantes recibidos desde este telefono"}


async def registrar_rut_empresa(telefono: str, rut: str) -> dict:
    rut_ok = normalizar_rut(rut)
    if not rut_ok:
        return {"resultado": "rut_invalido"}
    if rut_ok == config.rut_propio():
        return {"resultado": "rut_de_huno", "instruccion": "Ese es el RUT de HUNO (a quien se transfiere). "
                "Pide el RUT de la empresa que esta pagando."}
    await memoria.guardar_rut_declarado(telefono, rut_ok)
    resultados = await conciliacion.reevaluar_pendientes(telefono, estados=("esperando_rut",))
    for comp, r in resultados:
        if r.estado == "confirmado":
            await conciliacion.marcar_notificado(comp.id)  # se le dice en esta misma respuesta
    return {
        "resultado": "rut_registrado",
        "rut": formatear_rut(rut_ok),
        "comprobantes_reevaluados": [r.mensaje() for _, r in resultados],
        "instruccion": "Si hay comprobantes reevaluados, transmite esos mensajes tal cual al cliente.",
    }


async def elegir_empresa_comprobante(telefono: str, empresa: str) -> dict:
    async with async_session() as s:
        comp = (await s.execute(
            select(Comprobante).where(Comprobante.telefono == telefono, Comprobante.estado == "esperando_empresa")
            .order_by(Comprobante.id)
        )).scalars().first()
        if comp is None:
            return {"resultado": "sin_comprobantes_en_espera"}
        opciones = await conciliacion.opciones_empresa(s, comp)
    elegido = huno.elegir(empresa, opciones) if opciones else None
    if elegido is None:
        return {"resultado": "no_coincide", "opciones": huno.lista_opciones(opciones),
                "instruccion": "Muestra las opciones y pide que responda con el numero."}
    r = await conciliacion.elegir_empresa(comp.id, elegido.id)
    return {"resultado": r.estado, "mensaje_para_el_cliente": r.mensaje(),
            "instruccion": "Transmite este mensaje tal cual, sin agregar datos."}


async def escalar_a_persona(telefono: str, motivo: str, resumen: str) -> dict:
    esc = await escalamientos.crear(telefono, motivo, resumen)
    from agent import horario

    return {"resultado": "derivado", "caso": esc.id, "responde": horario.cuando_responde(),
            "instruccion": "Dile al cliente que un ejecutivo le respondera por este chat " + horario.cuando_responde() + "."}


async def ejecutar(nombre: str, entrada: dict, telefono: str) -> str:
    try:
        if nombre == "consultar_facturas_pendientes":
            r = await consultar_facturas_pendientes(telefono, (entrada.get("empresa") or None))
        elif nombre == "consultar_pagos_realizados":
            r = await consultar_pagos_realizados(telefono, (entrada.get("empresa") or None))
        elif nombre == "estado_comprobantes":
            r = await estado_comprobantes(telefono)
        elif nombre == "registrar_rut_empresa":
            r = await registrar_rut_empresa(telefono, str(entrada.get("rut", "")))
        elif nombre == "registrar_telefono":
            from agent import registro

            r = {"mensaje_para_el_cliente": await registro.validar_datos(
                telefono, str(entrada.get("rut", "")), str(entrada.get("correo", ""))),
                "instruccion": "Transmite este mensaje tal cual."}
        elif nombre == "elegir_empresa_comprobante":
            r = await elegir_empresa_comprobante(telefono, str(entrada.get("empresa", "")))
        elif nombre == "escalar_a_persona":
            motivo = entrada.get("motivo") if entrada.get("motivo") in escalamientos.MOTIVOS else "consulta"
            r = await escalar_a_persona(telefono, motivo, str(entrada.get("resumen", ""))[:2000])
        else:
            r = {"error": f"herramienta desconocida {nombre}"}
    except Exception as e:  # noqa: BLE001
        r = {"error": "fallo interno", "detalle": str(e)[:200]}
    return json.dumps(r, ensure_ascii=False)
