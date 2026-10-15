"""Pruebas del agente USP: importadores, conciliacion, webhook, panel y conversacion."""

import hashlib
import hmac
import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select

from agent import comprobantes, conciliacion, flujo, huno, importadores, llm, main, memoria
from agent.comprobantes import DatosComprobante
from agent.db import (AbonoHuno, ClienteHuno, Comprobante, Escalamiento, FacturaHuno, Movimiento, Pago,
                      async_session, hoy_chile)
from agent.providers.base import Adjunto, MensajeEntrante
from agent.utils import normalizar_rut, normalizar_telefono, parsear_monto

EJ = Path(__file__).resolve().parent.parent / "ejemplos"
TEL_MARIA = "56911112222"   # contacto de Comercial Atacama (76.123.456-0)
TEL_JUAN = "56933334444"    # contacto de Inversiones Elqui (77.654.321-7)
TEL_DESCONOCIDO = "56955556666"
FECHA = date(2026, 10, 10)


# ── helpers ──────────────────────────────────────────────────────────────


_ids = {"factura": 100}


async def nuevo_cliente(cid, rut, nombre, telefonos=None):
    """Simula una ficha de cliente de app.huno.cl."""
    async with async_session() as s:
        s.add(ClienteHuno(id=cid, rut=rut, nombre=nombre, nombre_empresa=nombre, telefonos=telefonos, activo=True))
        await s.commit()
    huno.invalidar_cache()


async def nueva_factura(cliente_id, folio, monto, pagado=False, emision=date(2026, 10, 1), abonos=()):
    """Simula una factura de Finanzas de app.huno.cl."""
    _ids["factura"] += 1
    async with async_session() as s:
        s.add(FacturaHuno(id=_ids["factura"], cliente_id=cliente_id, folio=folio, monto=monto, pagado=pagado,
                          fecha_emision=emision, anio=emision.year, mes=emision.month))
        for a in abonos:
            s.add(AbonoHuno(factura_id=_ids["factura"], monto=a))
        await s.commit()
    return _ids["factura"]


async def cargar_ejemplos(cartola=True):
    # Formatos distintos de clientes.telefonos, a proposito
    await nuevo_cliente(1, "76.123.456-0", "Comercial Atacama Ltda", "María Pérez +56 9 1111 2222")
    await nuevo_cliente(2, "77654321-7", "Inversiones Elqui SpA", '[{"nombre": "Juan Soto", "telefono": "9 3333 4444"}]')
    await nuevo_cliente(3, "76999888-8", "Transportes Copayapu SpA", None)
    await nueva_factura(1, "1501", 230000)
    await nueva_factura(2, "1502", 420000)
    await nueva_factura(3, "1503", 690000)
    await nueva_factura(1, "1450", 230000, pagado=True, emision=date(2026, 9, 1))
    if cartola:
        await importadores.importar_cartola((EJ / "cartola_bci_ejemplo.csv").read_bytes(), "c.csv")


def datos(monto, fecha=FECHA, op=None, cuenta="12-345-4321", rut_origen=None, es=True):
    return DatosComprobante(es_comprobante=es, legible=monto is not None, monto=monto, fecha=fecha,
                            numero_operacion=op, cuenta_destino=cuenta, rut_origen=rut_origen,
                            banco_origen="Banco Estado", nombre_origen="Cliente")


def mensaje(tel, texto="", url=None, evento="ev"):
    adj = [Adjunto(tipo="image", url=url, mime="image/jpeg")] if url else []
    return MensajeEntrante(telefono=tel, texto=texto, mensaje_id=evento, adjuntos=adj,
                           contexto={"evento_id": evento, "conversation_id": "conv-" + tel, "account_id": "acc"})


async def enviar_comprobante(proveedor, monkeypatch, tel, d, archivo=b"img", evento="ev1"):
    async def leer_falso(contenido, mime):
        return d
    monkeypatch.setattr(comprobantes, "leer", leer_falso)
    url = f"https://zernio.com/api/v1/whatsapp/media/{evento}"
    proveedor.archivos[url] = (b"\xff\xd8" + archivo, "image/jpeg")
    respuestas, _ = await flujo.procesar(proveedor, mensaje(tel, url=url, evento=evento))
    return respuestas


async def estado_factura(folio):
    """(factura, saldo) leidos de las tablas de app.huno.cl."""
    async with async_session() as s:
        f = (await s.execute(select(FacturaHuno).where(FacturaHuno.folio == folio))).scalars().one()
        abonos = (await s.execute(select(func.coalesce(func.sum(AbonoHuno.monto), 0))
                                  .where(AbonoHuno.factura_id == f.id))).scalar_one()
    return SimpleNamespace(id=f.id, pagado=bool(f.pagado), saldo=0 if f.pagado else f.monto - abonos,
                           fecha_pago=f.fecha_pago, usuario_pago=f.usuario_pago)


async def contar(modelo, *cond):
    async with async_session() as s:
        return (await s.execute(select(func.count()).select_from(modelo).where(*cond))).scalar_one()


# ── utilidades ───────────────────────────────────────────────────────────


def test_rut_monto_telefono():
    assert normalizar_rut("76.123.456-0") == "76123456-0"
    assert normalizar_rut("76.123.456-1") is None
    assert normalizar_rut("12345678-5") == "12345678-5"
    assert parsear_monto("$1.234.567") == 1234567
    assert parsear_monto("230.000,00") == 230000
    assert parsear_monto(230000.0) == 230000
    assert normalizar_telefono("+56 9 7870 1038") == "56978701038"
    assert normalizar_telefono("978701038") == "56978701038"


def test_interpretar_salida_del_modelo():
    d = comprobantes.interpretar({"es_comprobante": True, "legible": True, "monto": "$230.000",
                                  "fecha": "2026-10-10", "rut_origen": "76.123.456-0", "numero_operacion": " 884512 "})
    assert d.monto == 230000 and d.fecha == FECHA and d.rut_origen == "76123456-0" and d.numero_operacion == "884512"
    futura = comprobantes.interpretar({"es_comprobante": True, "legible": True, "monto": "1000",
                                       "fecha": (hoy_chile() + timedelta(days=5)).isoformat()})
    assert futura.fecha is None and futura.legible is False


# ── importadores ─────────────────────────────────────────────────────────


async def test_importar_cartola_sin_duplicar(db):
    contenido = (EJ / "cartola_bci_ejemplo.csv").read_bytes()
    r1 = await importadores.importar_cartola(contenido, "cartola.csv")
    assert r1.nuevos == 3 and r1.omitidos == 1, r1.dict()  # el cargo se omite
    r2 = await importadores.importar_cartola(contenido, "cartola.csv")
    assert r2.nuevos == 0
    async with async_session() as s:
        m = (await s.execute(select(Movimiento).where(Movimiento.monto == 230000))).scalars().one()
    assert m.rut_origen == "76123456-0" and m.documento == "884512" and m.disponible == 230000


async def test_importar_cartola_excel(db, tmp_path):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["Movimientos BCI"])
    ws.append(["Fecha", "Glosa", "Depósitos", "Giros", "Saldo"])
    ws.append([date(2026, 10, 13), "TRANSFERENCIA DE 12.345.678-5", 99000, None, 1000000])
    ruta = tmp_path / "c.xlsx"
    wb.save(ruta)
    r = await importadores.importar_cartola(ruta.read_bytes(), "c.xlsx")
    assert r.nuevos == 1, r.dict()


def test_parsear_telefonos_de_la_ficha():
    p = huno.parsear_telefonos
    assert p("María Pérez +56 9 1111 2222") == [("56911112222", "María Pérez")]
    assert p('[{"nombre": "Juan", "telefono": "9 3333 4444"}]') == [("56933334444", "Juan")]
    assert p("Juan: 912345678, Ana: +56 9 8765 4321") == [("56912345678", "Juan"), ("56987654321", "Ana")]
    assert p("+56912345678 (dueño)\n+56 9 2222 3333 contadora") == [("56912345678", "dueño"), ("56922223333", "contadora")]
    assert p('["+56 9 1111 2222", "+56 9 3333 4444"]') == [("56911112222", None), ("56933334444", None)]
    assert p("") == [] and p(None) == [] and p("sin telefono") == []
    # Formato de app.huno.cl: lista con la clave "valor"
    assert p('[{"valor": "+56 9 1111 2222", "nombre": "María Pérez"}]') == [("56911112222", "María Pérez")]
    assert p('[{"Valor": "+56911112222", "Nombre": "Juan"}, {"Valor": "+56 9 3333 4444", "Nombre": ""}]') == \
        [("56911112222", "Juan"), ("56933334444", None)]
    assert p('[{"tipo": "celular", "valor": "912345678"}]') == [("56912345678", None)]
    assert p('"[{\\"valor\\": \\"912345678\\", \\"nombre\\": \\"Ana\\"}]"') == [("56912345678", "Ana")]
    assert p('[{"valor": "912345678", "nombre": "[{x}]"}]') == [("56912345678", None)]


async def test_lee_clientes_y_saldo_con_abonos(db):
    await cargar_ejemplos(cartola=False)
    await nueva_factura(1, "1700", 100000, abonos=(30000, 20000))
    assert [c.nombre for c in await huno.clientes_de_telefono(TEL_JUAN)] == ["Inversiones Elqui SpA"]
    assert [c.id for c in await huno.clientes_de_rut("76123456-0")] == [1]
    async with async_session() as s:
        pend = await huno.facturas_pendientes(s, [1])
    assert [(f.folio, f.saldo) for f in pend] == [("1501", 230000), ("1700", 50000)]  # 1450 esta pagada


# ── conciliacion ─────────────────────────────────────────────────────────


async def test_confirma_pago_exacto(db, proveedor, monkeypatch):
    await cargar_ejemplos()
    resp = await enviar_comprobante(proveedor, monkeypatch, TEL_MARIA, datos(230000, op="884512"))
    assert "Pago confirmado" in resp[0] and "1501" in resp[0] and "$230.000" in resp[0]
    f = await estado_factura("1501")
    assert f.pagado and f.fecha_pago == date(2026, 10, 10) and f.usuario_pago == huno.USUARIO_AGENTE
    async with async_session() as s:
        m = (await s.execute(select(Movimiento).where(Movimiento.monto == 230000))).scalars().one()
    assert m.disponible == 0


async def test_mismo_comprobante_reenviado_no_paga_dos_veces(db, proveedor, monkeypatch):
    await cargar_ejemplos()
    await enviar_comprobante(proveedor, monkeypatch, TEL_MARIA, datos(230000, op="884512"), archivo=b"A", evento="e1")
    resp = await enviar_comprobante(proveedor, monkeypatch, TEL_MARIA, datos(230000, op="884512"), archivo=b"A", evento="e2")
    assert "Pago confirmado" in resp[0]  # se informa el estado del original
    assert await contar(Pago) == 1


async def test_comprobante_de_otro_chat_se_escala(db, proveedor, monkeypatch):
    await cargar_ejemplos()
    await enviar_comprobante(proveedor, monkeypatch, TEL_MARIA, datos(230000, op="884512"), archivo=b"A", evento="e1")
    resp = await enviar_comprobante(proveedor, monkeypatch, TEL_JUAN, datos(230000, op="884512"), archivo=b"A", evento="e2")
    assert "ejecutivo" in resp[0]
    assert await contar(Escalamiento, Escalamiento.motivo == "comprobante_reutilizado") == 1
    assert await contar(Pago) == 1


async def test_pendiente_banco_y_confirmacion_al_cargar_cartola(db, proveedor, monkeypatch):
    await cargar_ejemplos(cartola=False)
    hoy = hoy_chile()
    resp = await enviar_comprobante(proveedor, monkeypatch, TEL_JUAN, datos(420000, fecha=hoy))
    assert "Aún no se refleja" in resp[0]
    cartola = f"Fecha;Descripción;Abonos\n{hoy.strftime('%d/%m/%Y')};TRANSF DE 77.654.321-7;420.000\n".encode()
    await importadores.importar_cartola(cartola, "c.csv")
    resultados = await conciliacion.reevaluar_pendientes(estados=("pendiente_banco",))
    from agent import avisos

    aviso = await avisos.avisar_confirmaciones(proveedor, resultados)
    assert aviso == {"avisados": 1, "sin_avisar": 0}
    assert "Pago confirmado" in proveedor.enviados[-1][1]
    assert proveedor.enviados[-1][2]["conversation_id"] == "conv-" + TEL_JUAN


async def test_comprobante_viejo_sin_movimiento_se_escala(db, proveedor, monkeypatch):
    await cargar_ejemplos(cartola=False)
    resp = await enviar_comprobante(proveedor, monkeypatch, TEL_JUAN, datos(420000, fecha=hoy_chile() - timedelta(days=10)))
    assert "ejecutivo" in resp[0]
    assert await contar(Escalamiento, Escalamiento.motivo == "sin_movimiento") == 1


async def test_telefono_desconocido_pide_rut_y_confirma(db, proveedor, monkeypatch):
    await cargar_ejemplos()
    resp = await enviar_comprobante(proveedor, monkeypatch, TEL_DESCONOCIDO, datos(690000, fecha=date(2026, 10, 12)))
    assert "RUT de la empresa" in resp[0]
    respuestas, _ = await flujo.procesar(proveedor, mensaje(TEL_DESCONOCIDO, "es 76.999.888-8", evento="e9"))
    assert "Pago confirmado" in respuestas[0] and "1503" in respuestas[0]


async def test_rut_declarado_no_permite_ver_deuda(db):
    await cargar_ejemplos()
    from agent import herramientas

    await memoria.guardar_rut_declarado(TEL_DESCONOCIDO, "76123456-0")
    r = await herramientas.consultar_facturas_pendientes(TEL_DESCONOCIDO)
    assert r["resultado"] == "telefono_no_registrado" and "empresas" not in r
    assert "RUT de la empresa" in r["mensaje_para_el_cliente"]  # se le ofrece registrar el numero
    r = await herramientas.consultar_facturas_pendientes(TEL_MARIA)
    assert r["empresas"][0]["total_pendiente"] == "$230.000"


async def test_cuenta_destino_distinta_se_escala(db, proveedor, monkeypatch):
    await cargar_ejemplos()
    await enviar_comprobante(proveedor, monkeypatch, TEL_MARIA, datos(230000, cuenta="****9999"))
    assert await contar(Escalamiento, Escalamiento.motivo == "cuenta_destino_distinta") == 1
    assert not (await estado_factura("1501")).pagado


async def test_monto_alto_se_escala(db, proveedor, monkeypatch):
    await cargar_ejemplos()
    await enviar_comprobante(proveedor, monkeypatch, TEL_MARIA, datos(5_000_000))
    assert await contar(Escalamiento, Escalamiento.motivo == "monto_alto") == 1


async def test_sin_factura_exacta_se_escala(db, proveedor, monkeypatch):
    await cargar_ejemplos()
    await enviar_comprobante(proveedor, monkeypatch, TEL_MARIA, datos(100000))
    assert await contar(Escalamiento, Escalamiento.motivo == "sin_factura_exacta") == 1


async def test_no_es_comprobante(db, proveedor, monkeypatch):
    await cargar_ejemplos()
    resp = await enviar_comprobante(proveedor, monkeypatch, TEL_MARIA, datos(None, es=False))
    assert "no parece un comprobante" in resp[0] and "ejecutivo" in resp[0]
    # Puede ser un documento para otra area: queda guardado y lo ve una persona
    assert await contar(Escalamiento, Escalamiento.motivo == "documento_recibido") == 1
    assert await contar(Pago) == 0


async def test_dos_facturas_mismo_monto_paga_la_mas_antigua(db, proveedor, monkeypatch):
    await cargar_ejemplos()
    await nueva_factura(1, "1600", 230000, emision=date(2026, 11, 1))
    resp = await enviar_comprobante(proveedor, monkeypatch, TEL_MARIA, datos(230000, op="884512"))
    assert "1501" in resp[0]
    assert not (await estado_factura("1600")).pagado


async def test_movimientos_ambiguos_se_escalan(db, proveedor, monkeypatch):
    await cargar_ejemplos(cartola=False)
    cartola = ("Fecha;Descripción;Abonos\n10/10/2026;TRANSFERENCIA DE TERCEROS;230.000\n"
               "10/10/2026;OTRA TRANSFERENCIA;230.000\n").encode()
    await importadores.importar_cartola(cartola, "c.csv")
    await enviar_comprobante(proveedor, monkeypatch, TEL_MARIA, datos(230000))
    assert await contar(Escalamiento, Escalamiento.motivo == "movimiento_ambiguo") == 1


async def test_misma_transferencia_no_se_usa_dos_veces(db, proveedor, monkeypatch):
    """Dos clientes distintos dicen haber pagado con la misma transferencia del banco."""
    await cargar_ejemplos(cartola=False)
    await nuevo_cliente(11, "12.345.678-5", "Uno", "+56 9 7777 0001")
    await nuevo_cliente(12, "15.111.222-6", "Dos", "+56 9 7777 0002")
    await nueva_factura(11, "2001", 50000)
    await nueva_factura(12, "2002", 50000)
    await importadores.importar_cartola(b"Fecha;Descripcion;Abonos\n10/10/2026;TRANSFERENCIA;50.000\n", "c.csv")
    r1 = await enviar_comprobante(proveedor, monkeypatch, "56977770001", datos(50000), archivo=b"x1", evento="a")
    r2 = await enviar_comprobante(proveedor, monkeypatch, "56977770002", datos(50000), archivo=b"x2", evento="b")
    assert "Pago confirmado" in r1[0]
    assert "Pago confirmado" not in r2[0]
    assert await contar(Pago) == 1


# ── webhook ──────────────────────────────────────────────────────────────


def _payload_recibido(tel="+56911112222", texto="hola", adjuntos=None, evento="evt_1"):
    return {
        "id": evento, "event": "message.received",
        "message": {"id": "m1", "conversationId": "conv1", "platform": "whatsapp", "platformMessageId": "wamid.1",
                    "direction": "incoming", "text": texto, "attachments": adjuntos or [],
                    "sender": {"id": tel.lstrip("+"), "phoneNumber": tel, "name": "María"},
                    "sentAt": "2026-10-14T12:00:00Z", "isRead": False},
        "conversation": {"id": "conv1", "platformConversationId": "x", "participantId": tel.lstrip("+"), "status": "active"},
        "account": {"id": "acc1", "accountId": "acc1", "platform": "whatsapp", "username": "huno"},
        "timestamp": "2026-10-14T12:00:00Z",
    }


async def _cliente(proveedor):
    main.proveedor = proveedor
    main.app.state.proveedor = proveedor
    main.error_configuracion = None
    main.estado_db.update(ok=True, detalle="conectada")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test")


async def test_webhook_saludo_sin_tokens_y_deduplicacion(db, proveedor):
    async with await _cliente(proveedor) as c:
        r = await c.post("/webhook", content=json.dumps(_payload_recibido()))
        assert r.status_code == 200 and r.json()["encolados"] == 1
        r = await c.post("/webhook", content=json.dumps(_payload_recibido()))
        assert r.json()["encolados"] == 0
    assert len(proveedor.enviados) == 1
    tel, texto, ctx = proveedor.enviados[0]
    assert tel == TEL_MARIA and "asistente de USP" in texto and ctx["conversation_id"] == "conv1"


async def test_webhook_comprobante_de_punta_a_punta(db, proveedor, monkeypatch):
    await cargar_ejemplos()

    async def leer_falso(contenido, mime):
        assert contenido == b"%PDF-1.7 datos" and mime == "application/pdf"
        return datos(230000, op="884512")
    monkeypatch.setattr(comprobantes, "leer", leer_falso)
    url = "https://zernio.com/api/v1/whatsapp/media/123"
    proveedor.archivos[url] = (b"%PDF-1.7 datos", "application/octet-stream")
    adj = [{"type": "file", "url": url, "mimeType": "application/pdf"}]
    async with await _cliente(proveedor) as c:
        r = await c.post("/webhook", content=json.dumps(_payload_recibido(texto="", adjuntos=adj, evento="evt_c")))
        assert r.json()["encolados"] == 1
    assert "Pago confirmado" in proveedor.enviados[-1][1]
    assert (await memoria.obtener_historial(TEL_MARIA))[0]["content"] == "[Envió un comprobante (PDF) por $230.000: confirmado]"


async def test_ejecutivo_responde_desde_celular_pausa_agente(db, proveedor):
    async with await _cliente(proveedor) as c:
        await c.post("/webhook", content=json.dumps(_payload_recibido(evento="e1")))
        enviado_humano = _payload_recibido(evento="e2")
        enviado_humano["event"] = "message.sent"
        enviado_humano["message"].update(direction="outgoing", sentVia="human", text="Hola María, lo reviso")
        await c.post("/webhook", content=json.dumps(enviado_humano))
        await c.post("/webhook", content=json.dumps(_payload_recibido(texto="gracias", evento="e3")))
    assert len(proveedor.enviados) == 1  # solo el primer saludo
    assert await memoria.esta_pausada(TEL_MARIA)
    # Los mensajes del propio agente (sentVia=api) no pausan
    await memoria.reanudar(TEL_MARIA)
    async with await _cliente(proveedor) as c:
        propio = _payload_recibido(evento="e4")
        propio["event"] = "message.sent"
        propio["message"].update(direction="outgoing", sentVia="api")
        await c.post("/webhook", content=json.dumps(propio))
    assert not await memoria.esta_pausada(TEL_MARIA)


async def test_firma_obligatoria_en_produccion(db, monkeypatch):
    from agent.providers.zernio import ProveedorZernio

    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("ZERNIO_WEBHOOK_SECRET", "")
    sin_secreto = ProveedorZernio()
    async with await _cliente(sin_secreto) as c:
        r = await c.post("/webhook", content=json.dumps(_payload_recibido()))
        assert r.status_code == 401

    monkeypatch.setenv("ZERNIO_WEBHOOK_SECRET", "secreto")
    con_secreto = ProveedorZernio()
    con_secreto.api_key = ""  # no intentara enviar de verdad
    cuerpo = json.dumps(_payload_recibido(evento="firm")).encode()
    firma = hmac.new(b"secreto", cuerpo, hashlib.sha256).hexdigest()
    async with await _cliente(con_secreto) as c:
        assert (await c.post("/webhook", content=cuerpo, headers={"X-Zernio-Signature": "malo"})).status_code == 401
        assert (await c.post("/webhook", content=cuerpo, headers={"X-Zernio-Signature": firma})).status_code == 200


async def test_api_key_no_se_envia_a_otros_dominios(monkeypatch):
    from agent.providers import zernio

    vistos = []

    class ClienteFalso:
        def __init__(self, **kw): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): ...
        async def get(self, url, headers=None):
            vistos.append((url, headers))
            return SimpleNamespace(status_code=200, content=b"x", headers={"content-type": "image/png"}, text="")

    monkeypatch.setattr(zernio.httpx, "AsyncClient", ClienteFalso)
    p = zernio.ProveedorZernio()
    p.api_key = "sk_secreta"
    await p.descargar_adjunto(Adjunto(tipo="image", url="https://zernio.com/api/v1/whatsapp/media/1"))
    await p.descargar_adjunto(Adjunto(tipo="image", url="https://malicioso.example/img.png"))
    await p.descargar_adjunto(Adjunto(tipo="image", url="/v1/whatsapp/media/2"))
    assert vistos[0][1] == {"Authorization": "Bearer sk_secreta"}
    assert vistos[1][1] == {}
    assert vistos[2][0].startswith("https://zernio.com/") and vistos[2][1]


# ── panel ────────────────────────────────────────────────────────────────


H = {"X-Admin-Token": "token-de-prueba-123456", "X-Usuario": "Ana%20USP"}


async def test_panel_requiere_token(db, proveedor):
    async with await _cliente(proveedor) as c:
        assert (await c.get("/admin/api/resumen")).status_code == 401
        assert (await c.get("/admin/api/resumen", headers={"X-Admin-Token": "x"})).status_code == 401
        assert (await c.get("/admin/api/resumen", headers=H)).status_code == 200
        assert (await c.get("/admin")).status_code == 200


async def test_panel_cartola_confirma_y_avisa(db, proveedor, monkeypatch):
    await cargar_ejemplos(cartola=False)
    await memoria.registrar_entrante(TEL_JUAN, {"conversation_id": "conv-j", "account_id": "acc"}, None)
    await enviar_comprobante(proveedor, monkeypatch, TEL_JUAN, datos(420000, fecha=hoy_chile()))
    cartola = f"Fecha;Descripción;Abonos\n{hoy_chile().strftime('%d/%m/%Y')};TRANSF;420.000\n".encode()
    async with await _cliente(proveedor) as c:
        r = await c.post("/admin/api/cartola", headers=H, files={"archivo": ("cartola.csv", cartola, "text/csv")})
    assert r.status_code == 200, r.text
    assert r.json()["comprobantes_reevaluados"] == {"confirmado": 1} and r.json()["avisos"]["avisados"] == 1


async def test_panel_confirmacion_manual_pago_de_dos_facturas(db, proveedor, monkeypatch):
    # Con la regla de varias facturas apagada, un pago de dos facturas lo aplica una persona
    from agent import config
    monkeypatch.setitem(config.negocio().setdefault("reglas", {}), "pago_varias_facturas", False)
    await cargar_ejemplos(cartola=False)
    await nueva_factura(1, "1601", 100000)
    await importadores.importar_cartola(b"Fecha;Descripcion;Abonos\n10/10/2026;TRANSF;330.000\n", "c.csv")
    await enviar_comprobante(proveedor, monkeypatch, TEL_MARIA, datos(330000))  # no calza: se escala
    async with async_session() as s:
        cid = (await s.execute(select(Comprobante.id))).scalar_one()
        mid = (await s.execute(select(Movimiento.id))).scalar_one()
    f1, f2 = (await estado_factura("1501")).id, (await estado_factura("1601")).id
    async with await _cliente(proveedor) as c:
        cand = (await c.get(f"/admin/api/candidatos/{cid}", headers=H)).json()
        assert {f["folio"] for f in cand["facturas"]} == {"1501", "1601"}
        r1 = await c.post(f"/admin/api/comprobantes/{cid}/confirmar", headers=H,
                          json={"factura_id": f1, "movimiento_id": mid, "avisar_cliente": False})
        r2 = await c.post(f"/admin/api/comprobantes/{cid}/confirmar", headers=H,
                          json={"factura_id": f2, "movimiento_id": mid, "avisar_cliente": False})
        r3 = await c.post(f"/admin/api/comprobantes/{cid}/confirmar", headers=H,
                          json={"factura_id": f2, "movimiento_id": mid})
    assert r1.status_code == 200 and r2.status_code == 200, (r1.text, r2.text)
    assert r3.status_code == 409
    assert (await estado_factura("1501")).pagado and (await estado_factura("1601")).pagado
    async with async_session() as s:
        assert (await s.get(Movimiento, mid)).disponible == 0
        pago = (await s.execute(select(Pago).where(Pago.factura_id == f1))).scalars().one()
    assert pago.registrado_por == "Ana USP"
    assert await contar(Escalamiento, Escalamiento.estado == "abierto") == 0


# ── conversacion con Claude (simulado) ───────────────────────────────────


def _bloque(**kw):
    b = SimpleNamespace(**kw)
    b.model_dump = lambda exclude_none=True: {k: v for k, v in kw.items() if v is not None}
    return b


async def test_conversacion_usa_herramienta_de_facturas(db, monkeypatch):
    await cargar_ejemplos()
    llamadas = []

    async def crear_falso(**kw):
        llamadas.append(kw)
        if len(llamadas) == 1:
            return SimpleNamespace(stop_reason="tool_use", content=[
                _bloque(type="tool_use", id="t1", name="consultar_facturas_pendientes", input={})])
        resultado = json.loads(kw["messages"][-1]["content"][0]["content"])
        total = resultado["empresas"][0]["total_pendiente"]
        return SimpleNamespace(stop_reason="end_turn", content=[_bloque(type="text", text=f"Tiene {total} pendiente.")])

    monkeypatch.setattr(llm, "crear_mensaje", crear_falso)
    from agent import cerebro

    texto, guardar = await cerebro.responder(TEL_MARIA, "cuánto debo?")
    assert texto == "Tiene $230.000 pendiente." and guardar
    assert llamadas[0]["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "NUNCA CONFIRMAS UN PAGO" in llamadas[0]["system"][0]["text"]
    assert "Preguntas frecuentes" in llamadas[0]["system"][0]["text"]


async def test_error_de_claude_deriva_a_persona(db, monkeypatch):
    async def falla(**kw):
        raise RuntimeError("caido")
    monkeypatch.setattr(llm, "crear_mensaje", falla)
    from agent import cerebro

    texto, guardar = await cerebro.responder(TEL_MARIA, "necesito una nota de crédito")
    assert "ejecutivo" in texto and not guardar
    assert await contar(Escalamiento) == 1


@pytest.mark.parametrize("texto", ["Hola!", "buenas tardes", "gracias", "👍"])
async def test_reglas_rapidas(db, texto, monkeypatch):
    async def no_llamar(**kw):
        raise AssertionError("no deberia llamar a Claude")
    monkeypatch.setattr(llm, "crear_mensaje", no_llamar)
    from agent import cerebro

    texto_resp, _ = await cerebro.responder(TEL_MARIA, texto)
    assert texto_resp


# ── SDK real de Anthropic contra un servidor simulado ────────────────────


def _cliente_anthropic_simulado(respuestas, peticiones):
    from anthropic import AsyncAnthropic

    try:  # las versiones nuevas del SDK usan httpx2
        import httpx2 as hx
    except ImportError:
        hx = httpx

    def manejar(req):
        peticiones.append(json.loads(req.content))
        return hx.Response(200, json=respuestas.pop(0))

    return AsyncAnthropic(api_key="sk-ant-test", http_client=hx.AsyncClient(transport=hx.MockTransport(manejar)))


def _resp(content, stop="end_turn"):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-5-5", "content": content,
            "stop_reason": stop, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 5}}


async def test_sdk_real_ciclo_de_herramientas(db, monkeypatch):
    await cargar_ejemplos()
    peticiones = []
    respuestas = [
        _resp([{"type": "text", "text": "Reviso."},
               {"type": "tool_use", "id": "toolu_1", "name": "consultar_facturas_pendientes", "input": {}}], "tool_use"),
        _resp([{"type": "text", "text": "Tiene $230.000 pendiente."}]),
    ]
    monkeypatch.setattr(llm, "_cliente", _cliente_anthropic_simulado(respuestas, peticiones))
    from agent import cerebro

    texto, _ = await cerebro.responder(TEL_MARIA, "cuánto debo?")
    assert texto == "Tiene $230.000 pendiente."
    segunda = peticiones[1]
    assert segunda["output_config"] == {"effort": "low"}
    assert segunda["messages"][-2]["content"][1] == {"type": "tool_use", "id": "toolu_1",
                                                     "name": "consultar_facturas_pendientes", "input": {}}
    assert segunda["messages"][-1]["content"][0]["tool_use_id"] == "toolu_1"


async def test_sdk_real_lectura_de_comprobante(monkeypatch):
    peticiones = []
    respuestas = [_resp([{"type": "tool_use", "id": "toolu_2", "name": "registrar_comprobante",
                          "input": {"es_comprobante": True, "legible": True, "monto": "$420.000",
                                    "fecha": "2026-10-11", "numero_operacion": "993100"}}], "tool_use")]
    monkeypatch.setattr(llm, "_cliente", _cliente_anthropic_simulado(respuestas, peticiones))
    d = await comprobantes.leer(b"%PDF-1.4 falso", "application/pdf")
    assert d.monto == 420000 and d.numero_operacion == "993100"
    p = peticiones[0]
    assert p["tool_choice"] == {"type": "tool", "name": "registrar_comprobante"}
    assert p["messages"][0]["content"][0]["type"] == "document"
    assert await comprobantes.leer(b"x", "video/mp4") is None


async def test_concurrencia_misma_transferencia(db):
    """Dos comprobantes evaluados AL MISMO TIEMPO no pueden usar la misma plata."""
    import asyncio

    await nuevo_cliente(11, "12.345.678-5", "Uno", "+56 9 7777 0001")
    await nuevo_cliente(12, "15.111.222-6", "Dos", "+56 9 7777 0002")
    await nueva_factura(11, "3001", 70000)
    await nueva_factura(12, "3002", 70000)
    await importadores.importar_cartola(f"Fecha;Descripcion;Abonos\n{hoy_chile():%d/%m/%Y};TRANSF;70.000\n".encode(), "c.csv")
    ids = []
    async with async_session() as s:
        for i, tel in enumerate(["56977770001", "56977770002"]):
            c = Comprobante(telefono=tel, es_comprobante=True, monto=70000, fecha=hoy_chile(), sha256=f"h{i}")
            s.add(c)
            await s.flush()
            ids.append(c.id)
        await s.commit()
    resultados = await asyncio.gather(*(conciliacion.evaluar(i) for i in ids))
    assert sorted(r.estado for r in resultados).count("confirmado") == 1
    assert await contar(Pago) == 1


async def test_reenvio_de_comprobante_escalado_no_se_redecide(db, proveedor, monkeypatch):
    await cargar_ejemplos()
    r1 = await enviar_comprobante(proveedor, monkeypatch, TEL_MARIA, datos(5_000_000), archivo=b"Z", evento="z1")
    r2 = await enviar_comprobante(proveedor, monkeypatch, TEL_MARIA, datos(5_000_000), archivo=b"Z", evento="z2")
    assert "ejecutivo" in r1[0] and "ejecutivo" in r2[0]
    assert await contar(Escalamiento) == 1


async def test_adjunto_no_soportado_y_audio(db, proveedor):
    proveedor.archivos["u"] = (b"PK\x03\x04zip", "application/zip")
    r, _ = await flujo.procesar(proveedor, MensajeEntrante(
        telefono=TEL_MARIA, texto="planilla de sueldos", mensaje_id="x", adjuntos=[Adjunto(tipo="file", url="u")],
        contexto={}))
    # Un Word/Excel/ZIP puede ser para contabilidad o RRHH: se guarda y se deriva, no queda sin respuesta
    assert "ejecutivo" in r[0] and "captura o PDF" in r[0]
    async with async_session() as s:
        esc = (await s.execute(select(Escalamiento))).scalars().one()
        comp = await s.get(Comprobante, esc.comprobante_id)
    assert esc.motivo == "documento_recibido" and "planilla de sueldos" in esc.resumen
    assert comp.archivo == b"PK\x03\x04zip"
    r, _ = await flujo.procesar(proveedor, MensajeEntrante(
        telefono=TEL_MARIA, texto="", mensaje_id="y", adjuntos=[Adjunto(tipo="audio", url="a")], contexto={}))
    assert "audios" in r[0]


# ── integracion con app.huno.cl ──────────────────────────────────────────


async def test_saludo_usa_nombre_de_la_ficha(db, monkeypatch):
    await cargar_ejemplos(cartola=False)
    from agent import cerebro

    assert (await cerebro.regla_rapida(TEL_MARIA, "hola")).startswith("Hola María,")
    assert (await cerebro.regla_rapida(TEL_DESCONOCIDO, "hola")).startswith("Hola, le saluda")


async def test_pago_parcial_desde_panel_queda_como_abono(db, proveedor, monkeypatch):
    await cargar_ejemplos(cartola=False)
    await memoria.registrar_entrante(TEL_MARIA, {"conversation_id": "conv-m", "account_id": "acc"}, None)
    await importadores.importar_cartola(b"Fecha;Descripcion;Abonos\n10/10/2026;TRANSF;100.000\n", "c.csv")
    await enviar_comprobante(proveedor, monkeypatch, TEL_MARIA, datos(100000))  # no calza: se escala
    async with async_session() as s:
        cid = (await s.execute(select(Comprobante.id))).scalar_one()
        mid = (await s.execute(select(Movimiento.id))).scalar_one()
    fid = (await estado_factura("1501")).id
    async with await _cliente(proveedor) as c:
        r = await c.post(f"/admin/api/comprobantes/{cid}/confirmar", headers=H,
                         json={"factura_id": fid, "movimiento_id": mid, "avisar_cliente": True})
        clientes = (await c.get("/admin/api/clientes", headers=H)).json()
    assert r.status_code == 200 and r.json()["saldo_restante"] == 130000, r.text
    f = await estado_factura("1501")
    assert not f.pagado and f.saldo == 130000
    assert await contar(AbonoHuno, AbonoHuno.factura_id == fid, AbonoHuno.monto == 100000) == 1
    assert "Saldo pendiente de esa factura: $130.000" in proveedor.enviados[-1][1]
    atacama = next(x for x in clientes if x["id"] == 1)
    assert atacama["telefonos"] == [{"telefono": TEL_MARIA, "nombre": "María Pérez"}]


async def test_ia_sabe_quien_escribe(db, monkeypatch):
    await cargar_ejemplos(cartola=False)
    capturas = []

    async def crear_falso(**kw):
        capturas.append(kw["system"])
        return SimpleNamespace(stop_reason="end_turn", content=[_bloque(type="text", text="ok")])
    monkeypatch.setattr(llm, "crear_mensaje", crear_falso)
    from agent import cerebro

    await cerebro.responder(TEL_JUAN, "necesito ayuda con mi cuenta")
    await cerebro.responder(TEL_DESCONOCIDO, "necesito ayuda con un tema")
    assert "Juan Soto" in capturas[0][1]["text"] and "Inversiones Elqui SpA" in capturas[0][1]["text"]
    assert "NO esta registrado" in capturas[1][1]["text"]


# ── una persona con varias empresas ──────────────────────────────────────

TEL_DUENO = "56944445555"


async def cargar_dueno_con_dos_empresas():
    await nuevo_cliente(21, "12.345.678-5", "Panadería El Trigo SpA", '[{"valor": "+56 9 4444 5555", "nombre": "Pedro"}]')
    await nuevo_cliente(22, "15.111.222-6", "Transportes Pedro Ltda", '[{"valor": "+56 9 4444 5555", "nombre": "Pedro"}]')
    await nueva_factura(21, "5001", 230000)
    await nueva_factura(22, "5002", 230000)
    await nueva_factura(22, "5003", 99000)


async def test_consulta_de_deuda_pregunta_la_empresa(db):
    await cargar_dueno_con_dos_empresas()
    from agent import herramientas

    r = await herramientas.consultar_facturas_pendientes(TEL_DUENO)
    assert r["resultado"] == "elegir_empresa"
    assert r["opciones"] == "1. Panadería El Trigo SpA (12.345.678-5)\n2. Transportes Pedro Ltda (15.111.222-6)"
    r = await herramientas.consultar_facturas_pendientes(TEL_DUENO, "2")
    assert [e["empresa"] for e in r["empresas"]] == ["Transportes Pedro Ltda"]
    assert r["empresas"][0]["total_pendiente"] == "$329.000"
    assert r["empresas"][0]["facturas"][0]["vence"] == "2026-10-16"  # emitida 01-10 + 15 dias
    r = await herramientas.consultar_facturas_pendientes(TEL_DUENO, "la panaderia")
    assert [e["empresa"] for e in r["empresas"]] == ["Panadería El Trigo SpA"]
    r = await herramientas.consultar_facturas_pendientes(TEL_DUENO, "15.111.222-6")
    assert [e["empresa"] for e in r["empresas"]] == ["Transportes Pedro Ltda"]
    r = await herramientas.consultar_facturas_pendientes(TEL_DUENO, "todas")
    assert len(r["empresas"]) == 2
    r = await herramientas.consultar_facturas_pendientes(TEL_DUENO, "otra cosa")
    assert r["resultado"] == "elegir_empresa"


async def test_comprobante_ambiguo_pregunta_empresa_y_confirma(db, proveedor, monkeypatch):
    await cargar_dueno_con_dos_empresas()
    await importadores.importar_cartola(b"Fecha;Descripcion;Abonos\n10/10/2026;TRANSF;230.000\n", "c.csv")
    resp = await enviar_comprobante(proveedor, monkeypatch, TEL_DUENO, datos(230000))
    assert "¿A cuál corresponde el pago?" in resp[0]
    assert "1. Panadería El Trigo SpA" in resp[0] and "2. Transportes Pedro Ltda" in resp[0]
    assert await contar(Escalamiento) == 0

    r, _ = await flujo.procesar(proveedor, mensaje(TEL_DUENO, "no se", evento="x1"))
    assert "No logré identificar la empresa" in r[0]
    r, _ = await flujo.procesar(proveedor, mensaje(TEL_DUENO, "2", evento="x2"))
    assert "Pago confirmado" in r[0] and "5002" in r[0] and "Transportes Pedro Ltda" in r[0]
    assert (await estado_factura("5002")).pagado and not (await estado_factura("5001")).pagado


async def test_eleccion_se_conserva_si_espera_banco(db, proveedor, monkeypatch):
    await cargar_dueno_con_dos_empresas()
    await enviar_comprobante(proveedor, monkeypatch, TEL_DUENO, datos(230000, fecha=hoy_chile()))
    r, _ = await flujo.procesar(proveedor, mensaje(TEL_DUENO, "Panadería", evento="y1"))
    assert "Aún no se refleja" in r[0]
    await importadores.importar_cartola(f"Fecha;Descripcion;Abonos\n{hoy_chile():%d/%m/%Y};TRANSF;230.000\n".encode(), "c.csv")
    resultados = await conciliacion.reevaluar_pendientes(estados=("pendiente_banco",))
    assert [r.estado for _, r in resultados] == ["confirmado"]
    assert (await estado_factura("5001")).pagado and not (await estado_factura("5002")).pagado


async def test_si_solo_una_empresa_calza_no_pregunta(db, proveedor, monkeypatch):
    await cargar_dueno_con_dos_empresas()
    await importadores.importar_cartola(b"Fecha;Descripcion;Abonos\n10/10/2026;TRANSF;99.000\n", "c.csv")
    resp = await enviar_comprobante(proveedor, monkeypatch, TEL_DUENO, datos(99000))
    assert "Pago confirmado" in resp[0] and "5003" in resp[0]


async def test_si_la_base_falla_no_se_cae_el_servidor(db, proveedor, monkeypatch):
    async def falla():
        raise RuntimeError("(ECIRCUITBREAKER) too many authentication failures")
    monkeypatch.setattr(main, "inicializar_db", falla)
    async with await _cliente(proveedor) as c:
        assert await main.conectar_db() is False
        salud = (await c.get("/")).json()
        assert salud["status"] == "error" and "DATABASE_URL" in salud["base_de_datos"]
        assert (await c.post("/webhook", content=json.dumps(_payload_recibido()))).status_code == 503
    monkeypatch.undo()
    assert await main.conectar_db() is True
