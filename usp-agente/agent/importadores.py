# agent/importadores.py — Carga de cartola BCI, facturas pendientes y contactos
"""
Acepta CSV (separado por ; o ,) y Excel (.xlsx). Las columnas se reconocen por nombre,
sin importar mayusculas, tildes ni el orden, para tolerar los distintos formatos de
exportacion de BCI Pyme y de Facto.
"""

import csv
import hashlib
import io
import logging
from dataclasses import dataclass, field

from sqlalchemy import select

from agent.db import Cliente, Contacto, Factura, Movimiento, ahora, async_session
from agent.utils import (
    buscar_ruts,
    normalizar_rut,
    normalizar_telefono,
    parsear_fecha,
    parsear_monto,
    sin_tildes,
)

logger = logging.getLogger("usp")

# Alias de columnas (ya sin tildes y en minusculas)
COL_CARTOLA = {
    "fecha": ["fecha", "fecha movimiento", "fecha operacion", "fecha contable"],
    "descripcion": ["descripcion", "glosa", "detalle", "movimiento", "descripcion movimiento"],
    "abono": ["abono", "abonos", "deposito", "depositos", "abonos ($)", "ingreso", "ingresos", "monto abono"],
    "cargo": ["cargo", "cargos", "giro", "giros", "cargos ($)", "egreso"],
    "monto": ["monto", "importe", "monto ($)"],
    "documento": ["n documento", "nro documento", "documento", "n de documento", "numero documento",
                  "n operacion", "nro operacion", "numero operacion", "serial", "n doc"],
    "saldo": ["saldo", "saldo diario", "saldo contable", "saldo disponible"],
}
COL_FACTURAS = {
    "folio": ["folio", "n factura", "numero", "nro", "n documento", "nro factura", "numero factura", "n"],
    "tipo": ["tipo", "tipo dte", "tipo documento", "dte"],
    "rut": ["rut", "rut cliente", "rut receptor", "rut empresa"],
    "razon_social": ["razon social", "cliente", "nombre", "receptor", "razon social receptor", "empresa"],
    "monto": ["total", "monto", "monto total", "total factura", "valor"],
    "saldo": ["saldo", "saldo pendiente", "por pagar", "pendiente"],
    "emision": ["fecha", "emision", "fecha emision", "fecha documento"],
    "vencimiento": ["vencimiento", "fecha vencimiento", "vence"],
    "estado": ["estado", "estado pago"],
    "email": ["email", "correo", "e-mail", "mail"],
}
COL_CONTACTOS = {
    "telefono": ["telefono", "celular", "whatsapp", "fono", "movil"],
    "rut": ["rut", "rut cliente", "rut empresa"],
    "nombre": ["nombre", "contacto"],
    "razon_social": ["razon social", "empresa", "cliente"],
}


@dataclass
class ResultadoImportacion:
    leidos: int = 0
    nuevos: int = 0
    actualizados: int = 0
    omitidos: int = 0
    errores: list[str] = field(default_factory=list)

    def dict(self):
        return {"leidos": self.leidos, "nuevos": self.nuevos, "actualizados": self.actualizados,
                "omitidos": self.omitidos, "errores": self.errores[:30]}


def _filas(contenido: bytes, nombre: str) -> list[list]:
    if nombre.lower().endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook

        wb = load_workbook(io.BytesIO(contenido), read_only=True, data_only=True)
        hoja = wb.worksheets[0]
        return [list(f) for f in hoja.iter_rows(values_only=True)]
    for codif in ("utf-8-sig", "latin-1"):
        try:
            texto = contenido.decode(codif)
            break
        except UnicodeDecodeError:
            continue
    muestra = texto[:4000]
    sep = ";" if muestra.count(";") >= muestra.count(",") else ","
    if muestra.count("\t") > max(muestra.count(";"), muestra.count(",")):
        sep = "\t"
    return [fila for fila in csv.reader(io.StringIO(texto), delimiter=sep)]


def _norm(c) -> str:
    return " ".join(sin_tildes(str(c or "")).replace("°", " ").replace("º", " ").replace(".", " ")
                    .replace("#", " ").split())


def _mapear(cabecera: list, alias: dict) -> dict[str, int]:
    nombres = [_norm(c) for c in cabecera]
    mapa = {}
    for clave, opciones in alias.items():
        for i, n in enumerate(nombres):
            if n in opciones and i not in mapa.values():
                mapa[clave] = i
                break
    return mapa


def _buscar_cabecera(filas: list[list], alias: dict, requeridas: set[str]) -> tuple[int, dict]:
    for i, fila in enumerate(filas[:40]):
        mapa = _mapear(fila, alias)
        if requeridas <= set(mapa):
            return i, mapa
    raise ValueError(
        "No encontre la fila de titulos. Columnas necesarias: " + ", ".join(sorted(requeridas))
    )


def _celda(fila: list, mapa: dict, clave: str):
    i = mapa.get(clave)
    return fila[i] if i is not None and i < len(fila) else None


# ── Cartola ──────────────────────────────────────────────────────────────


async def importar_cartola(contenido: bytes, nombre: str) -> ResultadoImportacion:
    """Importa solo abonos (ingresos). Re-importar la misma cartola no duplica."""
    res = ResultadoImportacion()
    filas = _filas(contenido, nombre)
    try:
        inicio, mapa = _buscar_cabecera(filas, COL_CARTOLA, {"fecha", "descripcion"})
    except ValueError as e:
        res.errores.append(str(e))
        return res
    if "abono" not in mapa and "monto" not in mapa:
        res.errores.append("La cartola no tiene columna de abonos/depositos ni de monto")
        return res

    vistos: dict[str, int] = {}
    nuevos = []
    for fila in filas[inicio + 1:]:
        if not any(c not in (None, "") for c in fila):
            continue
        fecha = parsear_fecha(_celda(fila, mapa, "fecha"))
        if fecha is None:
            continue  # totales, subtitulos, pie de pagina
        res.leidos += 1
        if "abono" in mapa:
            monto = parsear_monto(_celda(fila, mapa, "abono"))
        else:
            monto = parsear_monto(_celda(fila, mapa, "monto"))
            if monto is not None and monto < 0:
                monto = None
        if not monto or monto <= 0:
            res.omitidos += 1
            continue
        desc = str(_celda(fila, mapa, "descripcion") or "").strip()
        doc = str(_celda(fila, mapa, "documento") or "").strip() or None
        saldo = str(_celda(fila, mapa, "saldo") or "").strip()
        base = f"{fecha.isoformat()}|{monto}|{desc}|{doc or ''}|{saldo}"
        vistos[base] = vistos.get(base, 0) + 1
        huella = hashlib.sha256(f"{base}|{vistos[base]}".encode()).hexdigest()
        ruts = buscar_ruts(desc)
        nuevos.append(Movimiento(fecha=fecha, monto=monto, disponible=monto, descripcion=desc[:2000], documento=doc,
                                 rut_origen=ruts[0] if ruts else None, huella=huella, fuente="cartola"))

    async with async_session() as s:
        existentes = set()
        huellas = [m.huella for m in nuevos]
        for i in range(0, len(huellas), 500):
            existentes |= set((await s.execute(select(Movimiento.huella).where(Movimiento.huella.in_(huellas[i:i + 500])))).scalars())
        for m in nuevos:
            if m.huella in existentes:
                res.omitidos += 1
                continue
            s.add(m)
            res.nuevos += 1
        await s.commit()
    return res


# ── Facturas ─────────────────────────────────────────────────────────────


async def importar_facturas(contenido: bytes, nombre: str, origen: str = "facto") -> ResultadoImportacion:
    """
    Crea o actualiza facturas pendientes. Nunca "despaga" una factura que el agente ya
    marco como pagada. Las facturas marcadas como pagadas en el archivo se marcan pagadas.
    """
    res = ResultadoImportacion()
    filas = _filas(contenido, nombre)
    try:
        inicio, mapa = _buscar_cabecera(filas, COL_FACTURAS, {"folio", "rut", "monto"})
    except ValueError as e:
        res.errores.append(str(e))
        return res

    async with async_session() as s:
        for n, fila in enumerate(filas[inicio + 1:], start=inicio + 2):
            if not any(c not in (None, "") for c in fila):
                continue
            folio = str(_celda(fila, mapa, "folio") or "").strip()
            if folio.endswith(".0"):
                folio = folio[:-2]
            rut = normalizar_rut(_celda(fila, mapa, "rut"))
            monto = parsear_monto(_celda(fila, mapa, "monto"))
            if not folio or not rut or not monto:
                if folio or rut:
                    res.errores.append(f"Fila {n}: folio, RUT o monto invalido")
                continue
            res.leidos += 1
            saldo = parsear_monto(_celda(fila, mapa, "saldo"))
            saldo = monto if saldo is None else saldo
            tipo = parsear_monto(_celda(fila, mapa, "tipo")) or 33
            estado_txt = sin_tildes(str(_celda(fila, mapa, "estado") or ""))
            pagada = saldo <= 0 or any(p in estado_txt for p in ("pagad", "cancelad"))
            anulada = "anulad" in estado_txt

            razon = str(_celda(fila, mapa, "razon_social") or "").strip() or rut
            cli = await s.get(Cliente, rut)
            if cli is None:
                s.add(Cliente(rut=rut, razon_social=razon[:300], email=_celda(fila, mapa, "email")))
            elif razon != rut and cli.razon_social != razon:
                cli.razon_social, cli.actualizado_en = razon[:300], ahora()

            fac = (await s.execute(select(Factura).where(Factura.tipo_dte == tipo, Factura.folio == folio))).scalars().first()
            estado = "anulada" if anulada else ("pagada" if pagada else "pendiente")
            if fac is None:
                s.add(Factura(tipo_dte=tipo, folio=folio, rut=rut, monto_total=monto, saldo=max(saldo, 0),
                              fecha_emision=parsear_fecha(_celda(fila, mapa, "emision")),
                              fecha_vencimiento=parsear_fecha(_celda(fila, mapa, "vencimiento")),
                              estado=estado, origen=origen))
                res.nuevos += 1
            elif fac.estado == "pagada" and estado == "pendiente":
                res.omitidos += 1  # el agente ya la cobro; el archivo esta desactualizado
            else:
                fac.monto_total, fac.saldo, fac.estado = monto, max(saldo, 0), estado
                fac.fecha_vencimiento = parsear_fecha(_celda(fila, mapa, "vencimiento")) or fac.fecha_vencimiento
                fac.actualizado_en = ahora()
                res.actualizados += 1
            await s.flush()
        await s.commit()
    return res


# ── Contactos ────────────────────────────────────────────────────────────


async def agregar_contacto(telefono: str, rut: str, nombre: str | None = None, razon_social: str | None = None) -> bool:
    tel, rut_ok = normalizar_telefono(telefono), normalizar_rut(rut)
    if not tel or not rut_ok:
        raise ValueError("Telefono o RUT invalido")
    async with async_session() as s:
        if await s.get(Cliente, rut_ok) is None:
            s.add(Cliente(rut=rut_ok, razon_social=(razon_social or rut_ok)[:300]))
            await s.flush()
        existe = (await s.execute(select(Contacto).where(Contacto.telefono == tel, Contacto.rut == rut_ok))).scalars().first()
        if existe:
            return False
        s.add(Contacto(telefono=tel, rut=rut_ok, nombre=nombre))
        await s.commit()
        return True


async def importar_contactos(contenido: bytes, nombre: str) -> ResultadoImportacion:
    res = ResultadoImportacion()
    filas = _filas(contenido, nombre)
    try:
        inicio, mapa = _buscar_cabecera(filas, COL_CONTACTOS, {"telefono", "rut"})
    except ValueError as e:
        res.errores.append(str(e))
        return res
    for n, fila in enumerate(filas[inicio + 1:], start=inicio + 2):
        tel, rut = _celda(fila, mapa, "telefono"), _celda(fila, mapa, "rut")
        if not tel and not rut:
            continue
        res.leidos += 1
        try:
            if await agregar_contacto(str(tel), str(rut), _celda(fila, mapa, "nombre"), _celda(fila, mapa, "razon_social")):
                res.nuevos += 1
            else:
                res.omitidos += 1
        except ValueError:
            res.errores.append(f"Fila {n}: telefono o RUT invalido")
    return res
