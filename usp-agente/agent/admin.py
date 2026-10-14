# agent/admin.py — API y panel para el equipo de USP
"""
Protegido con ADMIN_TOKEN (header X-Admin-Token). Sin ADMIN_TOKEN el panel queda apagado.
"""

import hmac
import os
from datetime import timedelta
from pathlib import Path
from urllib.parse import unquote

from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from agent import avisos, conciliacion, importadores, memoria
from agent.db import (
    Cliente,
    Comprobante,
    Contacto,
    Conversacion,
    Escalamiento,
    Factura,
    Movimiento,
    Pago,
    ahora,
    async_session,
    como_utc,
)
from agent.escalamientos import MOTIVOS
from agent.utils import formatear_monto

router = APIRouter()
PANEL = Path(__file__).parent / "static" / "panel.html"
TAMANO_MAXIMO_ARCHIVO = 10 * 1024 * 1024


def autorizar(x_admin_token: str = Header(default="")) -> None:
    token = os.getenv("ADMIN_TOKEN", "")
    if len(token) < 16:
        raise HTTPException(503, "Panel desactivado: configura ADMIN_TOKEN (minimo 16 caracteres)")
    if not hmac.compare_digest(token.encode(), (x_admin_token or "").encode()):
        raise HTTPException(401, "Token incorrecto")


def usuario(x_usuario: str = Header(default="")) -> str:
    return (unquote(x_usuario or "") or "panel")[:100]


@router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
async def panel():
    return HTMLResponse(PANEL.read_text(encoding="utf-8"),
                        headers={"X-Frame-Options": "DENY", "Cache-Control": "no-store",
                                 "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                 "script-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; frame-src blob:; object-src blob:"})


async def _leer(archivo: UploadFile) -> bytes:
    datos = await archivo.read()
    if len(datos) > TAMANO_MAXIMO_ARCHIVO:
        raise HTTPException(413, "Archivo demasiado grande (maximo 10 MB)")
    return datos


# ── Cargas ───────────────────────────────────────────────────────────────


@router.post("/admin/api/cartola", dependencies=[Depends(autorizar)])
async def subir_cartola(request: Request, archivo: UploadFile = File(...)):
    res = await importadores.importar_cartola(await _leer(archivo), archivo.filename or "cartola.csv")
    reevaluados = await conciliacion.reevaluar_pendientes(estados=("pendiente_banco",))
    aviso = await avisos.avisar_confirmaciones(request.app.state.proveedor, reevaluados)
    conteo: dict[str, int] = {}
    for _, r in reevaluados:
        conteo[r.estado] = conteo.get(r.estado, 0) + 1
    return {"importacion": res.dict(), "comprobantes_reevaluados": conteo, "avisos": aviso}


@router.post("/admin/api/facturas", dependencies=[Depends(autorizar)])
async def subir_facturas(archivo: UploadFile = File(...)):
    res = await importadores.importar_facturas(await _leer(archivo), archivo.filename or "facturas.csv")
    return {"importacion": res.dict()}


@router.post("/admin/api/contactos/archivo", dependencies=[Depends(autorizar)])
async def subir_contactos(archivo: UploadFile = File(...)):
    res = await importadores.importar_contactos(await _leer(archivo), archivo.filename or "contactos.csv")
    return {"importacion": res.dict()}


class NuevoContacto(BaseModel):
    telefono: str
    rut: str
    nombre: str | None = None
    razon_social: str | None = None


@router.post("/admin/api/contactos", dependencies=[Depends(autorizar)])
async def crear_contacto(datos: NuevoContacto):
    try:
        creado = await importadores.agregar_contacto(datos.telefono, datos.rut, datos.nombre, datos.razon_social)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    # Si tenia comprobantes esperando RUT, ahora se pueden asociar
    from agent.utils import normalizar_telefono

    reevaluados = await conciliacion.reevaluar_pendientes(normalizar_telefono(datos.telefono), estados=("esperando_rut",))
    return {"creado": creado, "comprobantes_reevaluados": len(reevaluados)}


# ── Consultas ────────────────────────────────────────────────────────────


@router.get("/admin/api/resumen", dependencies=[Depends(autorizar)])
async def resumen():
    async with async_session() as s:
        async def contar(modelo, *cond):
            return (await s.execute(select(func.count()).select_from(modelo).where(*cond))).scalar_one()

        hace_un_dia = ahora() - timedelta(days=1)
        return {
            "casos_abiertos": await contar(Escalamiento, Escalamiento.estado == "abierto"),
            "pendientes_banco": await contar(Comprobante, Comprobante.estado == "pendiente_banco"),
            "esperando_rut": await contar(Comprobante, Comprobante.estado == "esperando_rut"),
            "confirmados_24h": await contar(Comprobante, Comprobante.estado == "confirmado", Comprobante.actualizado_en >= hace_un_dia),
            "confirmados_sin_avisar": await contar(Comprobante, Comprobante.estado == "confirmado", Comprobante.notificado.is_(False)),
            "facturas_pendientes": await contar(Factura, Factura.estado == "pendiente"),
            "ultimo_movimiento": (await s.execute(select(func.max(Movimiento.fecha)))).scalar_one(),
            "clientes": await contar(Cliente),
            "contactos": await contar(Contacto),
        }


def _comp_dict(c: Comprobante) -> dict:
    return {
        "id": c.id, "telefono": c.telefono, "estado": c.estado, "motivo": c.motivo,
        "monto": c.monto, "monto_txt": formatear_monto(c.monto) if c.monto else None,
        "fecha": c.fecha.isoformat() if c.fecha else None, "numero_operacion": c.numero_operacion,
        "banco_origen": c.banco_origen, "rut_origen": c.rut_origen, "nombre_origen": c.nombre_origen,
        "cuenta_destino": c.cuenta_destino, "factura_id": c.factura_id, "movimiento_id": c.movimiento_id,
        "notificado": c.notificado, "creado_en": como_utc(c.creado_en).isoformat() if c.creado_en else None,
        "tiene_archivo": c.archivo is not None,
    }


@router.get("/admin/api/escalamientos", dependencies=[Depends(autorizar)])
async def listar_escalamientos(estado: str = "abierto", limite: int = 100):
    async with async_session() as s:
        filas = (await s.execute(
            select(Escalamiento).where(Escalamiento.estado == estado).order_by(Escalamiento.id.desc()).limit(min(limite, 500))
        )).scalars().all()
        salida = []
        for e in filas:
            conv = await s.get(Conversacion, e.telefono)
            comp = await s.get(Comprobante, e.comprobante_id) if e.comprobante_id else None
            salida.append({
                "id": e.id, "telefono": e.telefono, "motivo": e.motivo, "motivo_txt": MOTIVOS.get(e.motivo, e.motivo),
                "resumen": e.resumen, "estado": e.estado, "creado_en": como_utc(e.creado_en).isoformat(),
                "nombre_whatsapp": conv.nombre_whatsapp if conv else None,
                "pausado": bool(conv and conv.pausado_hasta and como_utc(conv.pausado_hasta) > ahora()),
                "comprobante": _comp_dict(comp) if comp else None,
            })
    return salida


class Resolucion(BaseModel):
    nota: str | None = None


@router.post("/admin/api/escalamientos/{eid}/resolver", dependencies=[Depends(autorizar)])
async def resolver(eid: int, datos: Resolucion, quien: str = Depends(usuario)):
    async with async_session() as s:
        e = await s.get(Escalamiento, eid)
        if e is None:
            raise HTTPException(404, "No existe")
        e.estado, e.resuelto_en, e.resuelto_por, e.nota_resolucion = "resuelto", ahora(), quien, datos.nota
        await s.commit()
    return {"ok": True}


@router.get("/admin/api/comprobantes", dependencies=[Depends(autorizar)])
async def listar_comprobantes(estado: str | None = None, sin_avisar: bool = False, limite: int = 100):
    q = select(Comprobante).order_by(Comprobante.id.desc()).limit(min(limite, 500))
    if estado:
        q = q.where(Comprobante.estado == estado)
    if sin_avisar:
        q = q.where(Comprobante.estado == "confirmado", Comprobante.notificado.is_(False))
    async with async_session() as s:
        return [_comp_dict(c) for c in (await s.execute(q)).scalars().all()]


@router.get("/admin/api/comprobantes/{cid}/archivo", dependencies=[Depends(autorizar)])
async def ver_archivo(cid: int):
    async with async_session() as s:
        c = await s.get(Comprobante, cid)
        if c is None or c.archivo is None:
            raise HTTPException(404, "Sin archivo")
        return Response(c.archivo, media_type=c.mime or "application/octet-stream",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/admin/api/candidatos/{cid}", dependencies=[Depends(autorizar)])
async def candidatos(cid: int):
    """Facturas y movimientos posibles para confirmar a mano un comprobante."""
    async with async_session() as s:
        c = await s.get(Comprobante, cid)
        if c is None:
            raise HTTPException(404, "No existe")
        ruts = await conciliacion.ruts_de_telefono(s, c.telefono)
        conv = await s.get(Conversacion, c.telefono)
        for extra in (c.rut_origen, conv.rut_declarado if conv else None):
            if extra and extra not in ruts:
                ruts.append(extra)
        fq = select(Factura).where(Factura.estado == "pendiente")
        fq = fq.where(Factura.rut.in_(ruts)) if ruts else (fq.where(Factura.saldo == c.monto) if c.monto else fq.where(False))
        facturas = (await s.execute(fq.order_by(Factura.fecha_vencimiento).limit(50))).scalars().all()
        mq = select(Movimiento).where(Movimiento.disponible > 0)
        if c.monto:
            mq = mq.where(Movimiento.monto == c.monto)
        movs = (await s.execute(mq.order_by(Movimiento.fecha.desc()).limit(50))).scalars().all()
        clientes = {r.rut: r.razon_social for r in (await s.execute(select(Cliente).where(Cliente.rut.in_({f.rut for f in facturas})))).scalars()}
        return {
            "facturas": [{"id": f.id, "folio": f.folio, "rut": f.rut, "razon_social": clientes.get(f.rut),
                          "saldo": f.saldo, "saldo_txt": formatear_monto(f.saldo),
                          "vencimiento": f.fecha_vencimiento.isoformat() if f.fecha_vencimiento else None} for f in facturas],
            "movimientos": [{"id": m.id, "fecha": m.fecha.isoformat(), "monto": m.monto, "monto_txt": formatear_monto(m.monto),
                             "disponible": m.disponible, "disponible_txt": formatear_monto(m.disponible),
                             "descripcion": m.descripcion, "documento": m.documento} for m in movs],
        }


class ConfirmacionManual(BaseModel):
    factura_id: int
    movimiento_id: int
    avisar_cliente: bool = True


@router.post("/admin/api/comprobantes/{cid}/confirmar", dependencies=[Depends(autorizar)])
async def confirmar_manual(cid: int, datos: ConfirmacionManual, request: Request, quien: str = Depends(usuario)):
    """Un ejecutivo confirma a mano (ej. pago parcial o de varias facturas se resuelve factura por factura)."""
    async with async_session() as s:
        c = await s.get(Comprobante, cid)
        f = await s.get(Factura, datos.factura_id)
        m = await s.get(Movimiento, datos.movimiento_id)
        if not c or not f or not m:
            raise HTTPException(404, "Comprobante, factura o movimiento no existe")
        if f.estado != "pendiente":
            raise HTTPException(409, "La factura no esta pendiente")
        aplicar = min(f.saldo, m.disponible)
        if aplicar <= 0:
            raise HTTPException(409, "Ese movimiento del banco ya fue usado por completo")
        try:
            rm = await s.execute(update(Movimiento).where(Movimiento.id == m.id, Movimiento.disponible >= aplicar)
                                 .values(disponible=Movimiento.disponible - aplicar))
            rf = await s.execute(update(Factura).where(Factura.id == f.id, Factura.saldo == f.saldo).values(
                saldo=f.saldo - aplicar, estado="pagada" if f.saldo == aplicar else "pendiente", actualizado_en=ahora()))
            if rm.rowcount != 1 or rf.rowcount != 1:
                raise IntegrityError("cambio concurrente", None, None)
            s.add(Pago(factura_id=f.id, movimiento_id=m.id, comprobante_id=c.id, monto=aplicar, origen="manual",
                       registrado_por=quien))
            nuevo_saldo = f.saldo - aplicar
            c.estado, c.factura_id, c.movimiento_id, c.motivo = "confirmado", f.id, m.id, f"manual: {quien}"
            c.actualizado_en = ahora()
            await s.commit()
        except IntegrityError as e:
            await s.rollback()
            raise HTTPException(409, "La factura o el movimiento cambio; recarga e intenta de nuevo") from e
        # Cerrar los casos abiertos de ese comprobante
        await s.execute(update(Escalamiento).where(Escalamiento.comprobante_id == cid, Escalamiento.estado == "abierto")
                        .values(estado="resuelto", resuelto_en=ahora(), resuelto_por=quien, nota_resolucion="confirmado a mano"))
        await s.commit()
        cliente = await s.get(Cliente, f.rut)
    aviso = {"avisados": 0, "sin_avisar": 0}
    if datos.avisar_cliente:
        r = conciliacion.Resultado("confirmado", "", aplicar, f.folio, cliente.razon_social if cliente else f.rut, f.rut)
        aviso = await avisos.avisar_confirmaciones(request.app.state.proveedor, [(c, r)])
    return {"ok": True, "saldo_restante": nuevo_saldo, "aviso": aviso}


@router.post("/admin/api/conversaciones/{telefono}/reanudar", dependencies=[Depends(autorizar)])
async def reanudar(telefono: str):
    await memoria.reanudar(telefono)
    return {"ok": True}


@router.post("/admin/api/conversaciones/{telefono}/pausar", dependencies=[Depends(autorizar)])
async def pausar(telefono: str, horas: int = 12):
    await memoria.pausar_por_humano("", telefono, max(1, min(horas, 72)))
    return {"ok": True}
