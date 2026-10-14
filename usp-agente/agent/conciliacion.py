# agent/conciliacion.py — Decide si un comprobante confirma un pago
"""
Toda la decision es deterministica. Un pago se confirma solo si TODO esto se cumple:

  1. El archivo es un comprobante legible (monto y fecha).
  2. No es un comprobante ya usado (mismo archivo o mismo N° de operacion).
  3. El monto no supera el maximo de confirmacion automatica.
  4. La cuenta de destino (si se lee) es de HUNO.
  5. Se sabe a que empresa corresponde (telefono registrado, RUT declarado o RUT de origen).
  6. Hay una factura pendiente de esa empresa con saldo EXACTO igual al monto
     (si hay varias del mismo monto, se paga la mas antigua).
  7. Hay UNA transferencia en el banco por ese monto, en el rango de fechas, aun no usada.

Si falta solo el banco (paso 7) y el comprobante es reciente, queda "pendiente_banco" y se
reintenta al cargar la cartola. Cualquier otra falla va a una persona.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError

from agent import config, escalamientos
from agent.db import (
    Cliente,
    Comprobante,
    Contacto,
    Conversacion,
    Factura,
    Movimiento,
    Pago,
    ahora,
    async_session,
    hoy_chile,
)
from agent.utils import formatear_monto, solo_alfanumerico

logger = logging.getLogger("usp")


@dataclass
class Resultado:
    estado: str          # confirmado | pendiente_banco | esperando_rut | escalado | no_legible | no_es_comprobante | duplicado
    motivo: str = ""
    monto: int | None = None
    folio: str | None = None
    razon_social: str | None = None
    rut: str | None = None

    def mensaje(self) -> str:
        campos = {
            "monto": formatear_monto(self.monto) if self.monto else "",
            "folio": self.folio or "",
            "razon_social": self.razon_social or "",
            "rut": self.rut or "",
        }
        if self.estado == "escalado" and not self.monto:
            return config.respuesta("recibido_revision")
        return config.respuesta(self.estado, **campos)


async def ruts_de_telefono(s, telefono: str) -> list[str]:
    return list((await s.execute(select(Contacto.rut).where(Contacto.telefono == telefono))).scalars().all())


async def _candidatos_rut(s, comp: Comprobante) -> list[str]:
    ruts = await ruts_de_telefono(s, comp.telefono)
    conv = await s.get(Conversacion, comp.telefono)
    for extra in (conv.rut_declarado if conv else None, comp.rut_origen):
        if extra and extra not in ruts and await s.get(Cliente, extra):
            ruts.append(extra)
    return ruts


def _cuenta_destino_ok(comp: Comprobante) -> bool:
    finales = config.cuentas_destino()
    if not finales or not comp.cuenta_destino:
        return True
    digitos = "".join(ch for ch in comp.cuenta_destino if ch.isdigit())
    if len(digitos) < 4:
        return True  # cuenta muy enmascarada: no se puede comparar
    return digitos[-4:] in finales


def _coincide_operacion(mov: Movimiento, numero: str | None) -> bool:
    op = solo_alfanumerico(numero)
    if len(op) < 4:
        return False
    return op in solo_alfanumerico(mov.documento) or op in solo_alfanumerico(mov.descripcion)


async def _escalar(s, comp: Comprobante, motivo: str, detalle: str = "") -> Resultado:
    comp.estado = "escalado"
    comp.motivo = motivo
    comp.actualizado_en = ahora()
    await s.commit()
    resumen = (
        f"Comprobante #{comp.id}: {formatear_monto(comp.monto) if comp.monto else 'monto ?'}"
        f" del {comp.fecha or '?'}; op {comp.numero_operacion or '-'};"
        f" origen {comp.nombre_origen or '-'} {comp.rut_origen or ''}. {detalle}"
    ).strip()
    await escalamientos.crear(comp.telefono, motivo, resumen, comp.id)
    return Resultado("escalado", motivo, comp.monto)


async def evaluar(comprobante_id: int) -> Resultado:
    reglas = config.reglas()
    async with async_session() as s:
        comp = await s.get(Comprobante, comprobante_id)
        if comp is None:
            raise ValueError(f"Comprobante {comprobante_id} no existe")
        if comp.estado == "confirmado":
            fac = await s.get(Factura, comp.factura_id) if comp.factura_id else None
            cli = await s.get(Cliente, fac.rut) if fac else None
            return Resultado("confirmado", "", comp.monto, fac.folio if fac else None,
                             cli.razon_social if cli else None, fac.rut if fac else None)

        # 1. Legibilidad
        if comp.es_comprobante is False:
            comp.estado, comp.motivo = "no_legible", "no_es_comprobante"
            await s.commit()
            return Resultado("no_es_comprobante")
        if not comp.monto or not comp.fecha:
            comp.estado, comp.motivo = "no_legible", "faltan_monto_o_fecha"
            await s.commit()
            return Resultado("no_legible")

        # 2. Reutilizacion del mismo comprobante
        condiciones = []
        if comp.sha256:
            condiciones.append(Comprobante.sha256 == comp.sha256)
        if comp.numero_operacion and len(solo_alfanumerico(comp.numero_operacion)) >= 4:
            condiciones.append(and_(Comprobante.numero_operacion == comp.numero_operacion,
                                    Comprobante.monto == comp.monto))
        if condiciones:
            previos = (
                await s.execute(
                    select(Comprobante).where(Comprobante.id != comp.id, or_(*condiciones)).order_by(Comprobante.id)
                )
            ).scalars().all()
            otro_chat = next((p for p in previos if p.telefono != comp.telefono), None)
            if otro_chat:
                return await _escalar(s, comp, "comprobante_reutilizado",
                                      f"El mismo comprobante llego desde +{otro_chat.telefono} (#{otro_chat.id}).")
            mismo_chat = next((p for p in previos if p.estado != "duplicado"), None)
            if mismo_chat:
                # El cliente reenvio el mismo comprobante: se informa el estado del original
                comp.estado, comp.motivo = "duplicado", f"reenvio_de_{mismo_chat.id}"
                await s.commit()
                if mismo_chat.estado not in ("pendiente_banco", "esperando_rut", "recibido"):
                    # Confirmado, en revision por una persona o ilegible: no se vuelve a decidir
                    if mismo_chat.estado == "confirmado":
                        original = mismo_chat.id
                    else:
                        estado = "escalado" if mismo_chat.estado == "escalado" else "no_legible"
                        return Resultado(estado, mismo_chat.motivo or "", mismo_chat.monto)
                else:
                    original = mismo_chat.id
            else:
                original = None
        else:
            original = None
    if original is not None:
        return await evaluar(original)

    async with async_session() as s:
        comp = await s.get(Comprobante, comprobante_id)

        # 3. Monto maximo
        if comp.monto > int(reglas["monto_maximo_auto"]):
            return await _escalar(s, comp, "monto_alto")

        # 4. Cuenta de destino
        if not _cuenta_destino_ok(comp):
            return await _escalar(s, comp, "cuenta_destino_distinta", f"Cuenta leida: {comp.cuenta_destino}")

        # 5. Empresa
        ruts = await _candidatos_rut(s, comp)
        if not ruts:
            conv = await s.get(Conversacion, comp.telefono)
            if conv and conv.rut_declarado:
                return await _escalar(s, comp, "cliente_no_encontrado", f"RUT declarado: {conv.rut_declarado}")
            comp.estado, comp.motivo, comp.actualizado_en = "esperando_rut", "", ahora()
            await s.commit()
            return Resultado("esperando_rut", "", comp.monto)

        # 6. Factura con saldo exacto
        facturas = (
            await s.execute(
                select(Factura)
                .where(Factura.rut.in_(ruts), Factura.estado == "pendiente", Factura.saldo == comp.monto)
                .order_by(Factura.fecha_vencimiento.is_(None), Factura.fecha_vencimiento,
                          Factura.fecha_emision, Factura.id)
            )
        ).scalars().all()
        if not facturas:
            return await _escalar(s, comp, "sin_factura_exacta", f"RUT revisados: {', '.join(ruts)}")
        if len({f.rut for f in facturas}) > 1:
            return await _escalar(s, comp, "factura_ambigua", f"RUT con ese monto: {', '.join(sorted({f.rut for f in facturas}))}")
        factura = facturas[0]

        # 7. Movimiento en el banco
        desde = comp.fecha - timedelta(days=int(reglas["dias_antes"]))
        hasta = comp.fecha + timedelta(days=int(reglas["dias_despues"]))
        movs = (
            await s.execute(
                select(Movimiento)
                .where(Movimiento.monto == comp.monto, Movimiento.disponible == comp.monto,
                       Movimiento.fecha >= desde, Movimiento.fecha <= hasta)
                .order_by(Movimiento.fecha, Movimiento.id)
            )
        ).scalars().all()

        if not movs:
            if (hoy_chile() - comp.fecha).days <= int(reglas["dias_espera_banco"]):
                comp.estado, comp.motivo, comp.actualizado_en = "pendiente_banco", "", ahora()
                await s.commit()
                return Resultado("pendiente_banco", "", comp.monto)
            return await _escalar(s, comp, "sin_movimiento", f"Factura candidata: N° {factura.folio}")

        movimiento = None
        por_operacion = [m for m in movs if _coincide_operacion(m, comp.numero_operacion)]
        if len(por_operacion) == 1:
            movimiento = por_operacion[0]
        else:
            ruts_validos = set(ruts) | ({comp.rut_origen} if comp.rut_origen else set())
            por_rut = [m for m in movs if m.rut_origen and m.rut_origen in ruts_validos]
            if len(por_rut) == 1:
                movimiento = por_rut[0]
            elif len(movs) == 1:
                unico = movs[0]
                if unico.rut_origen and unico.rut_origen not in ruts_validos:
                    return await _escalar(s, comp, "movimiento_rut_distinto",
                                          f"Movimiento #{unico.id} viene de {unico.rut_origen}")
                movimiento = unico
        if movimiento is None:
            return await _escalar(s, comp, "movimiento_ambiguo", f"{len(movs)} movimientos de {formatear_monto(comp.monto)}")

        # Aplicar en una sola transaccion. Los dos UPDATE condicionales (plata disponible en el
        # movimiento y saldo de la factura) impiden pagar dos veces aunque lleguen dos
        # comprobantes al mismo tiempo desde chats distintos.
        try:
            rm = await s.execute(
                update(Movimiento)
                .where(Movimiento.id == movimiento.id, Movimiento.disponible == comp.monto)
                .values(disponible=0)
            )
            if rm.rowcount != 1:
                raise IntegrityError("movimiento usado", None, None)
            s.add(Pago(factura_id=factura.id, movimiento_id=movimiento.id, comprobante_id=comp.id,
                       monto=comp.monto, origen="agente"))
            r = await s.execute(
                update(Factura)
                .where(Factura.id == factura.id, Factura.estado == "pendiente", Factura.saldo == comp.monto)
                .values(saldo=0, estado="pagada", actualizado_en=ahora())
            )
            if r.rowcount != 1:
                raise IntegrityError("factura cambio", None, None)
            comp.estado, comp.motivo = "confirmado", ""
            comp.factura_id, comp.movimiento_id = factura.id, movimiento.id
            comp.actualizado_en = ahora()
            await s.commit()
        except IntegrityError:
            await s.rollback()
            comp = await s.get(Comprobante, comprobante_id)
            return await _escalar(s, comp, "conflicto")

        cliente = await s.get(Cliente, factura.rut)
        logger.info(f"Pago confirmado: comprobante #{comp.id} -> factura {factura.folio} / movimiento #{movimiento.id}")
        return Resultado("confirmado", "", comp.monto, factura.folio,
                         cliente.razon_social if cliente else factura.rut, factura.rut)


async def reevaluar_pendientes(telefono: str | None = None, estados=("pendiente_banco", "esperando_rut")) -> list[tuple[Comprobante, Resultado]]:
    """Vuelve a evaluar comprobantes en espera (al cargar cartola o al recibir un RUT)."""
    async with async_session() as s:
        q = select(Comprobante.id).where(Comprobante.estado.in_(estados))
        if telefono:
            q = q.where(Comprobante.telefono == telefono)
        ids = list((await s.execute(q.order_by(Comprobante.id))).scalars().all())
    salida = []
    for cid in ids:
        res = await evaluar(cid)
        async with async_session() as s:
            salida.append((await s.get(Comprobante, cid), res))
    return salida
