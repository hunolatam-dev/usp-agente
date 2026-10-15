"""Escenarios donde antes el cliente podia quedar sin respuesta, y reglas nuevas del mundo HUNO."""

import asyncio
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, update

from agent import cerebro, comprobantes, conciliacion, config, flujo, herramientas, horario, importadores, llm, main, memoria, tareas
from agent.db import Comprobante, Conversacion, Escalamiento, Mensaje, Movimiento, Pago, ahora, async_session, hoy_chile
from agent.providers.base import Adjunto, MensajeEntrante
from tests.test_agente import (
    TEL_DESCONOCIDO, TEL_DUENO, TEL_JUAN, TEL_MARIA, _bloque, _cliente, _payload_recibido, cargar_dueno_con_dos_empresas,
    cargar_ejemplos, contar, datos, enviar_comprobante, estado_factura, mensaje, nueva_factura,
)

CL = ZoneInfo("America/Santiago")


def ia_falsa(monkeypatch, texto="Respuesta de la IA"):
    """Reemplaza a Claude; devuelve la lista de llamadas para revisarlas."""
    llamadas = []

    async def crear(**kw):
        llamadas.append(kw)
        return SimpleNamespace(stop_reason="end_turn", content=[_bloque(type="text", text=texto)] if texto else [])
    monkeypatch.setattr(llm, "crear_mensaje", crear)
    return llamadas


def sin_ia(monkeypatch):
    async def no_llamar(**kw):
        raise AssertionError("no deberia llamar a Claude")
    monkeypatch.setattr(llm, "crear_mensaje", no_llamar)


def regla(monkeypatch, clave, valor):
    monkeypatch.setitem(config.negocio().setdefault("reglas", {}), clave, valor)


async def ultimo_caso(motivo=None):
    async with async_session() as s:
        q = select(Escalamiento).order_by(Escalamiento.id.desc())
        if motivo:
            q = q.where(Escalamiento.motivo == motivo)
        return (await s.execute(q)).scalars().first()


# ── Horario: cuando responde una persona ────────────────────────────────


@pytest.mark.parametrize("momento, esperado", [
    (datetime(2026, 10, 13, 10, 0, tzinfo=CL), "hoy, dentro del horario de atención"),
    (datetime(2026, 10, 13, 20, 0, tzinfo=CL), "mañana miércoles a partir de las 9:00 hrs"),
    (datetime(2026, 10, 16, 17, 45, tzinfo=CL), "el lunes 19 a partir de las 9:00 hrs"),   # viernes cierra 17:30
    (datetime(2026, 10, 19, 7, 30, tzinfo=CL), "hoy a partir de las 9:00 hrs"),
    (datetime(2026, 12, 24, 18, 30, tzinfo=CL), "el lunes 28 a partir de las 9:00 hrs"),   # 25 feriado
    (datetime(2026, 12, 7, 19, 0, tzinfo=CL), "el miércoles 9 a partir de las 9:00 hrs"),  # 8 feriado
])
def test_cuando_responde_un_ejecutivo(momento, esperado):
    assert horario.cuando_responde(momento) == esperado


def test_mensajes_de_derivacion_dicen_cuando():
    texto = config.respuesta("escalado_texto")
    assert "{cuando}" not in texto and ("hoy" in texto or "a partir de las" in texto)


# ── Conocimiento de HUNO ─────────────────────────────────────────────────


def test_prompt_conoce_servicios_precio_y_sitios():
    p = config.system_prompt()
    for x in ("- Contabilidad", "- Gestión de RRHH", "- Administración de empresas", "- Marketing digital",
              "- Sistemas y desarrollo web", "$55.000", "soporte informático", "Estado de", "clientes.huno.cl",
              "huno.cl/constituir", 'motivo "cotizacion"', "Reserva tu hora"):
        assert x in p, x
    assert "{{" not in p


def test_con_cotizador_el_prompt_entrega_el_enlace(monkeypatch):
    monkeypatch.setitem(config.negocio(), "cotizador_url", "https://cotiza.huno.cl")
    config.system_prompt.cache_clear()
    try:
        assert "https://cotiza.huno.cl" in config.system_prompt()
    finally:
        config.system_prompt.cache_clear()


@pytest.mark.parametrize("texto, contiene", [
    ("a qué cuenta transfiero?", "89635451"),
    ("Hola, me pasa los datos de transferencia", "77.663.555-3"),
    ("¿Cuál es el horario de atención?", "lunes a jueves"),
    ("qué servicios tienen?", "$55.000"),
    ("cuánto cuesta el servicio de contabilidad", "Contabilidad"),
])
async def test_preguntas_frecuentes_sin_tokens(db, monkeypatch, texto, contiene):
    sin_ia(monkeypatch)
    r, _ = await cerebro.responder(TEL_MARIA, texto)
    assert contiene in r


async def test_pedir_una_persona_deriva_sin_tokens(db, monkeypatch):
    sin_ia(monkeypatch)
    r, _ = await cerebro.responder(TEL_MARIA, "Quiero hablar con una persona")
    assert "ejecutivo" in r
    assert (await ultimo_caso()).motivo == "pedido_cliente"


@pytest.mark.parametrize("texto", ["quiero dar de baja mi plan", "cuánto debo de mi factura?",
                                   "pagué en cuotas, a qué cuenta?", "me equivoqué de cuenta al transferir"])
async def test_frases_con_matiz_van_a_la_ia(db, monkeypatch, texto):
    await cargar_ejemplos(cartola=False)
    llamadas = ia_falsa(monkeypatch)
    await cerebro.responder(TEL_MARIA, texto)
    assert len(llamadas) == 1


# ── Mensajes cortos segun contexto ───────────────────────────────────────


async def test_ok_despues_de_una_pregunta_lo_interpreta_la_ia(db, monkeypatch):
    llamadas = ia_falsa(monkeypatch, "Listo, lo derivé.")
    await memoria.guardar_turno(TEL_MARIA, "quiero cambiar mi plan", "¿Quiere que lo derive a un ejecutivo?")
    r, _ = await cerebro.responder(TEL_MARIA, "ok")
    assert r == "Listo, lo derivé." and len(llamadas) == 1
    await memoria.guardar_turno(TEL_MARIA, "ok", "Listo, quedó derivado.")
    r, _ = await cerebro.responder(TEL_MARIA, "ok")
    assert r.startswith("A usted") and len(llamadas) == 1


async def test_sticker_y_video_tienen_respuesta(db, proveedor):
    r, _ = await flujo.procesar(proveedor, MensajeEntrante(
        telefono=TEL_MARIA, texto="", mensaje_id="s", adjuntos=[Adjunto(tipo="sticker", url="s")], contexto={}))
    assert r and r[0].startswith("A usted")
    r, _ = await flujo.procesar(proveedor, MensajeEntrante(
        telefono=TEL_MARIA, texto="", mensaje_id="v", adjuntos=[Adjunto(tipo="video", url="v")], contexto={}))
    assert "videos" in r[0]


async def test_respuesta_vacia_de_la_ia_deriva(db, monkeypatch):
    ia_falsa(monkeypatch, texto="")
    r, guardar = await cerebro.responder(TEL_MARIA, "tengo una duda rara")
    assert "ejecutivo" in r and not guardar
    assert (await ultimo_caso()).motivo == "error_tecnico"


async def test_limite_de_consultas_por_hora(db, monkeypatch):
    ia_falsa(monkeypatch)
    regla(monkeypatch, "max_consultas_ia_hora", 2)
    cerebro._uso_ia.clear()
    for i in range(2):
        r, _ = await cerebro.responder(TEL_MARIA, f"consulta número {i}")
        assert r == "Respuesta de la IA"
    r, _ = await cerebro.responder(TEL_MARIA, "otra consulta más")
    assert "dejé su conversación a un ejecutivo" in r
    cerebro._uso_ia.clear()


def test_salida_sin_markdown():
    assert cerebro._para_whatsapp("**Total:** $1.000\n## Detalle\nok") == "*Total:* $1.000\nDetalle\nok"


async def test_ia_sabe_horario_casos_y_comprobantes_en_espera(db, proveedor, monkeypatch):
    await cargar_dueno_con_dos_empresas()
    await importadores.importar_cartola(b"Fecha;Descripcion;Abonos\n10/10/2026;TRANSF;230.000\n", "c.csv")
    await enviar_comprobante(proveedor, monkeypatch, TEL_DUENO, datos(230000))
    await herramientas.escalar_a_persona(TEL_DUENO, "reclamo", "molesto")
    ctx = await cerebro.contexto(TEL_DUENO)
    assert "Ahora en Chile" in ctx and "Casos abiertos" in ctx and "Reclamo" in ctx
    assert "esperando que el cliente indique la empresa" in ctx and "1. Panadería El Trigo SpA" in ctx


# ── Eleccion de empresa de un comprobante ────────────────────────────────


async def _comprobante_esperando_empresa(proveedor, monkeypatch):
    await cargar_dueno_con_dos_empresas()
    await importadores.importar_cartola(b"Fecha;Descripcion;Abonos\n10/10/2026;TRANSF;230.000\n", "c.csv")
    resp = await enviar_comprobante(proveedor, monkeypatch, TEL_DUENO, datos(230000))
    assert "¿A cuál corresponde el pago?" in resp[0]


async def test_gracias_no_se_toma_como_eleccion_y_recuerda(db, proveedor, monkeypatch):
    await _comprobante_esperando_empresa(proveedor, monkeypatch)
    sin_ia(monkeypatch)
    r, _ = await flujo.procesar(proveedor, mensaje(TEL_DUENO, "gracias", evento="g1"))
    assert r[0].startswith("A usted") and "sigo esperando que me indique" in r[0] and "No logré" not in r[0]
    r, _ = await flujo.procesar(proveedor, mensaje(TEL_DUENO, "hola", evento="g2"))
    assert "1. Panadería El Trigo SpA" in r[0]


async def test_ninguna_de_las_empresas_deriva(db, proveedor, monkeypatch):
    await _comprobante_esperando_empresa(proveedor, monkeypatch)
    r, _ = await flujo.procesar(proveedor, mensaje(TEL_DUENO, "ninguna", evento="n1"))
    assert "ejecutivo" in r[0]
    assert (await ultimo_caso()).motivo == "factura_ambigua"


async def test_si_la_conversacion_siguio_decide_la_ia_con_su_herramienta(db, proveedor, monkeypatch):
    await _comprobante_esperando_empresa(proveedor, monkeypatch)
    await memoria.guardar_turno(TEL_DUENO, "y cuánto debo en total?", "¿De cuál empresa quiere consultar?")
    llamadas = ia_falsa(monkeypatch)
    await flujo.procesar(proveedor, mensaje(TEL_DUENO, "2", evento="h1"))
    assert len(llamadas) == 1 and not (await estado_factura("5002")).pagado
    r = await herramientas.elegir_empresa_comprobante(TEL_DUENO, "Transportes")
    assert r["resultado"] == "confirmado" and "5002" in r["mensaje_para_el_cliente"]
    async with async_session() as s:
        comp = (await s.execute(select(Comprobante))).scalars().one()
    assert comp.notificado  # se le dijo en la misma respuesta: la revision no lo vuelve a mandar


# ── Archivos ─────────────────────────────────────────────────────────────


async def test_texto_con_pregunta_junto_al_comprobante_se_responde(db, proveedor, monkeypatch):
    await cargar_ejemplos()
    llamadas = ia_falsa(monkeypatch, "Sí, con ese pago la empresa queda al día.")

    async def leer(contenido, mime):
        return datos(230000, op="884512")
    monkeypatch.setattr(comprobantes, "leer", leer)
    proveedor.archivos["u1"] = (b"\xff\xd8img", "image/jpeg")
    r, turno = await flujo.procesar(proveedor, mensaje(TEL_MARIA, "¿con esto quedo al día?", url="u1", evento="c1"))
    assert "Pago confirmado" in r[0] and r[1].startswith("Sí, con ese pago")
    assert "el sistema ya le respondió" in llamadas[0]["messages"][-1]["content"]
    assert "Envió un comprobante" in turno and "¿con esto quedo al día?" in turno


async def test_texto_simple_junto_al_comprobante_no_gasta_tokens(db, proveedor, monkeypatch):
    await cargar_ejemplos()
    sin_ia(monkeypatch)
    r = await enviar_comprobante(proveedor, monkeypatch, TEL_MARIA, datos(230000, op="884512"))
    assert len(r) == 1


async def test_rut_junto_al_comprobante_lo_asocia_sin_preguntar(db, proveedor, monkeypatch):
    await cargar_ejemplos()

    async def leer(contenido, mime):
        return datos(690000, fecha=date(2026, 10, 12))
    monkeypatch.setattr(comprobantes, "leer", leer)
    proveedor.archivos["u2"] = (b"\xff\xd8x", "image/jpeg")
    r, _ = await flujo.procesar(proveedor, mensaje(TEL_DESCONOCIDO, "Pago empresa 76.999.888-8", url="u2", evento="r1"))
    assert len(r) == 1 and "Pago confirmado" in r[0] and "1503" in r[0]


async def test_rut_sin_guion_para_comprobante_en_espera(db, proveedor, monkeypatch):
    await cargar_ejemplos()
    resp = await enviar_comprobante(proveedor, monkeypatch, TEL_DESCONOCIDO, datos(690000, fecha=date(2026, 10, 12)))
    assert "RUT de la empresa" in resp[0]
    r, _ = await flujo.procesar(proveedor, mensaje(TEL_DESCONOCIDO, "769998888", evento="r2"))
    assert "Pago confirmado" in r[0]


async def test_dos_comprobantes_ilegibles_seguidos_los_ve_una_persona(db, proveedor, monkeypatch):
    await cargar_ejemplos()
    r1 = await enviar_comprobante(proveedor, monkeypatch, TEL_MARIA, datos(None), archivo=b"1", evento="i1")
    r2 = await enviar_comprobante(proveedor, monkeypatch, TEL_MARIA, datos(None), archivo=b"2", evento="i2")
    assert "No logré leer" in r1[0] and "ejecutivo" in r2[0]
    assert (await ultimo_caso()).motivo == "error_lectura"


async def test_archivo_que_no_se_descarga_queda_derivado(db, proveedor):
    r, _ = await flujo.procesar(proveedor, mensaje(TEL_MARIA, url="https://zernio.com/api/v1/whatsapp/media/perdido"))
    assert "ejecutivo" in r[0]
    assert (await ultimo_caso()).motivo == "error_lectura"


# ── Pago de varias facturas con una transferencia ────────────────────────


async def test_pago_de_las_facturas_mas_antiguas_se_confirma_todo(db, proveedor, monkeypatch):
    await cargar_ejemplos(cartola=False)
    await nueva_factura(1, "1601", 100000, emision=date(2026, 10, 5))
    await importadores.importar_cartola(b"Fecha;Descripcion;Abonos\n10/10/2026;TRANSF;330.000\n", "c.csv")
    r = await enviar_comprobante(proveedor, monkeypatch, TEL_MARIA, datos(330000), archivo=b"V")
    assert "Facturas N° 1501 y 1601" in r[0] and "quedaron pagadas" in r[0] and "$330.000" in r[0]
    assert (await estado_factura("1501")).pagado and (await estado_factura("1601")).pagado
    assert await contar(Pago) == 2
    async with async_session() as s:
        assert (await s.execute(select(Movimiento.disponible))).scalar_one() == 0
    # Reenviar el mismo comprobante informa lo mismo y no paga dos veces
    r = await enviar_comprobante(proveedor, monkeypatch, TEL_MARIA, datos(330000), archivo=b"V", evento="ev2")
    assert "Facturas N° 1501 y 1601" in r[0] and await contar(Pago) == 2
    estado = await herramientas.estado_comprobantes(TEL_MARIA)
    assert estado["comprobantes"][0]["factura"] == "1501 y 1601"


async def test_suma_de_facturas_que_no_son_las_mas_antiguas_la_ve_una_persona(db, proveedor, monkeypatch):
    await cargar_ejemplos(cartola=False)
    await nueva_factura(1, "1601", 100000, emision=date(2026, 10, 5))
    await nueva_factura(1, "1602", 50000, emision=date(2026, 10, 6))
    await importadores.importar_cartola(b"Fecha;Descripcion;Abonos\n10/10/2026;TRANSF;150.000\n", "c.csv")
    await enviar_comprobante(proveedor, monkeypatch, TEL_MARIA, datos(150000))
    assert (await ultimo_caso()).motivo == "sin_factura_exacta" and await contar(Pago) == 0


# ── Webhook y envio ──────────────────────────────────────────────────────


async def test_si_algo_falla_el_cliente_igual_recibe_respuesta(db, proveedor, monkeypatch):
    async def explota(*a, **k):
        raise RuntimeError("base caida a medias")
    monkeypatch.setattr(flujo, "procesar", explota)
    main.proveedor = proveedor
    await main.procesar_mensaje(mensaje(TEL_MARIA, "hola", evento="f1"))
    assert "problema técnico" in proveedor.enviados[-1][1]
    assert (await ultimo_caso()).motivo == "error_tecnico"


async def test_si_whatsapp_no_acepta_el_envio_se_reintenta_y_deriva(db, proveedor, monkeypatch):
    monkeypatch.setattr(main, "REINTENTOS_ENVIO", (0, 0))
    proveedor.falla_envio = True
    main.proveedor = proveedor
    await main.procesar_mensaje(mensaje(TEL_MARIA, "hola", evento="f2"))
    caso = await ultimo_caso("error_tecnico")
    assert caso and "No se pudo enviar" in caso.resumen


async def test_mensajes_seguidos_se_responden_juntos(db, proveedor, monkeypatch):
    await cargar_ejemplos(cartola=False)
    llamadas = ia_falsa(monkeypatch, "Tiene $230.000 pendiente.")
    monkeypatch.setattr(main, "AGRUPAR_SEGUNDOS", 0.05)
    main.proveedor = proveedor
    m1, m2 = mensaje(TEL_MARIA, "hola", evento="a1"), mensaje(TEL_MARIA, "cuánto debo?", evento="a2")

    async def despues(m):
        await asyncio.sleep(0.01)
        await main.procesar_mensaje(m)
    await asyncio.gather(main.procesar_mensaje(m1), despues(m2))
    assert len(llamadas) == 1 and llamadas[0]["messages"][-1]["content"] == "hola\ncuánto debo?"
    assert [t for _, t, _ in proveedor.enviados] == ["Tiene $230.000 pendiente."]


def _saliente(evento, via, **extra):
    p = _payload_recibido(evento=evento)
    p["event"] = "message.sent"
    p["message"].update(direction="outgoing", sentVia=via, text="Gracias por escribir, le responderemos pronto", **extra)
    return p


async def test_respuesta_automatica_de_whatsapp_business_no_pausa(db, proveedor):
    async with await _cliente(proveedor) as c:
        await c.post("/webhook", content=json.dumps(_payload_recibido(evento="w1")))
        await c.post("/webhook", content=json.dumps(_saliente("w2", None, source="whatsapp_business_app")))
        assert not await memoria.esta_pausada(TEL_MARIA)
        for i, (via, extra) in enumerate([("workflow", {}), ("broadcast", {}), (None, {"source": "meta_business_agent"})]):
            await c.post("/webhook", content=json.dumps(_saliente(f"w3{i}", via, **extra)))
        assert not await memoria.esta_pausada(TEL_MARIA)
        # Lo mismo, pero mucho despues del ultimo mensaje del cliente: es una persona en el celular
        async with async_session() as s:
            await s.execute(update(Conversacion).values(ultimo_entrante_en=ahora() - timedelta(hours=1)))
            await s.commit()
        await c.post("/webhook", content=json.dumps(_saliente("w4", None, source="whatsapp_business_app")))
    assert await memoria.esta_pausada(TEL_MARIA)
    hist = await memoria.obtener_historial(TEL_MARIA)
    assert hist[-1]["role"] == "assistant" and "[Respondió un ejecutivo de USP]" in hist[-1]["content"]


async def test_mensaje_en_standby_no_se_responde(db, proveedor):
    p = _payload_recibido(evento="sb")
    p["metadata"] = {"standby": True}
    assert await proveedor.parsear_webhook(p) == []


async def test_con_ejecutivo_atendiendo_igual_se_avisa_el_pago(db, proveedor, monkeypatch):
    await cargar_ejemplos()
    await memoria.registrar_entrante(TEL_MARIA, {"conversation_id": "conv-" + TEL_MARIA}, None)
    await memoria.pausar_por_humano("conv-" + TEL_MARIA, TEL_MARIA, 12)
    r = await enviar_comprobante(proveedor, monkeypatch, TEL_MARIA, datos(230000, op="884512"))
    assert len(r) == 1 and "Pago confirmado" in r[0]
    r, turno = await flujo.procesar(proveedor, mensaje(TEL_MARIA, "¿me confirma si llegó?", evento="p2"))
    assert r == [] and turno is None
    async with async_session() as s:
        guardado = (await s.execute(select(Mensaje).order_by(Mensaje.id.desc()))).scalars().first()
    assert guardado.role == "user" and "me confirma" in guardado.content


# ── Revision periodica ───────────────────────────────────────────────────


async def test_revision_deriva_si_el_banco_no_muestra_el_pago(db, proveedor, monkeypatch):
    await cargar_ejemplos(cartola=False)
    await enviar_comprobante(proveedor, monkeypatch, TEL_JUAN, datos(420000, fecha=hoy_chile()))
    async with async_session() as s:
        await s.execute(update(Comprobante).values(fecha=hoy_chile() - timedelta(days=10)))
        await s.commit()
    await tareas.revisar(proveedor)
    assert (await ultimo_caso()).motivo == "sin_movimiento"


async def test_revision_deriva_si_el_cliente_no_responde_y_se_cierra_al_confirmar(db, proveedor, monkeypatch):
    await cargar_ejemplos()
    await enviar_comprobante(proveedor, monkeypatch, TEL_DESCONOCIDO, datos(690000, fecha=date(2026, 10, 12)))
    async with async_session() as s:
        await s.execute(update(Comprobante).values(actualizado_en=ahora() - timedelta(hours=7)))
        await s.commit()
    await tareas.revisar(proveedor)
    await tareas.revisar(proveedor)
    assert await contar(Escalamiento, Escalamiento.motivo == "cliente_no_respondio") == 1
    r, _ = await flujo.procesar(proveedor, mensaje(TEL_DESCONOCIDO, "76.999.888-8", evento="z9"))
    assert "Pago confirmado" in "\n".join(r)
    caso = await ultimo_caso("cliente_no_respondio")
    assert caso.estado == "resuelto" and "Confirmado automáticamente" in caso.nota_resolucion


async def test_revision_avisa_pagos_confirmados_que_no_se_pudieron_avisar(db, proveedor, monkeypatch):
    await cargar_ejemplos(cartola=False)
    await enviar_comprobante(proveedor, monkeypatch, TEL_JUAN, datos(420000, fecha=hoy_chile()))
    await importadores.importar_cartola(
        f"Fecha;Descripcion;Abonos\n{hoy_chile():%d/%m/%Y};TRANSF;420.000\n".encode(), "c.csv")
    await conciliacion.reevaluar_pendientes(estados=("pendiente_banco",))  # confirmado, sin avisar
    r = await tareas.revisar(proveedor)
    assert r["avisos_tardios"]["avisados"] == 1
    assert "Pago confirmado" in proveedor.enviados[-1][1]
    r = await tareas.revisar(proveedor)
    assert r["avisos_tardios"]["avisados"] == 0


async def test_panel_puede_correr_la_revision(db, proveedor):
    from tests.test_agente import H

    async with await _cliente(proveedor) as c:
        r = await c.post("/admin/api/revisar", headers=H)
    assert r.status_code == 200 and "derivados_sin_respuesta" in r.json()


# ── Archivos: 180 dias, respaldo por correo al dia 170 ───────────────────


async def _archivo(dias, tel=TEL_MARIA, contenido=b"%PDF-1.4 x", respaldado=False):
    async with async_session() as s:
        c = Comprobante(telefono=tel, archivo=contenido, mime="application/pdf", estado="confirmado", monto=1000,
                        creado_en=ahora() - timedelta(days=dias),
                        respaldado_en=ahora() - timedelta(days=dias - 170) if respaldado else None)
        s.add(c)
        await s.commit()
        return c.id


async def _comp(cid):
    async with async_session() as s:
        return await s.get(Comprobante, cid)


async def test_respaldo_al_dia_170_y_borrado_al_180(db, monkeypatch):
    from agent import respaldo

    enviados = []

    async def enviar(asunto, html, adjuntos):
        enviados.append((asunto, html, adjuntos))
        return True, "ok"
    monkeypatch.setattr(respaldo, "_enviar", enviar)
    nuevo, a_respaldar, viejo = await _archivo(10), await _archivo(171), await _archivo(181, respaldado=True)
    r = await respaldo.respaldar_y_limpiar()
    assert r["respaldados"] == 1 and r["borrados"] == 1
    asunto, html, adjuntos = enviados[0]
    assert "1 archivo(s)" in asunto and adjuntos[0]["nombre"].endswith(f"_{a_respaldar}.pdf")
    assert "Se borra del sistema" in html
    assert (await _comp(a_respaldar)).respaldado_en is not None and (await _comp(a_respaldar)).archivo
    assert (await _comp(viejo)).archivo is None and (await _comp(viejo)).monto == 1000  # los datos se quedan
    assert (await _comp(nuevo)).archivo and (await _comp(nuevo)).respaldado_en is None
    assert (await respaldo.respaldar_y_limpiar())["respaldados"] == 0  # no se respalda dos veces


async def test_sin_respaldo_nunca_se_borra_y_se_avisa(db, monkeypatch):
    from agent import respaldo

    monkeypatch.delenv("RESPALDO_TOKEN", raising=False)
    cid = await _archivo(185)
    r = await respaldo.respaldar_y_limpiar()
    assert r["borrados"] == 0 and r["sin_respaldo_vencidos"] == 1 and "RESPALDO_TOKEN" in r["errores"][0]
    assert (await _comp(cid)).archivo
    caso = await ultimo_caso("error_tecnico")
    assert caso.telefono == "sistema" and "NO se borraron" in caso.resumen


async def test_respaldo_en_varios_correos_si_son_pesados(db, monkeypatch):
    from agent import respaldo

    lotes = []

    async def enviar(asunto, html, adjuntos):
        lotes.append(len(adjuntos))
        return True, "ok"
    monkeypatch.setattr(respaldo, "_enviar", enviar)
    monkeypatch.setattr(respaldo, "LOTE_BYTES", 25)
    for _ in range(3):
        await _archivo(172, contenido=b"%PDF-" + b"x" * 15)
    assert (await respaldo.respaldar_y_limpiar())["respaldados"] == 3
    assert lotes == [1, 1, 1]


async def test_tareas_diarias_corren_una_vez_desde_las_7(db, monkeypatch):
    from agent import respaldo

    llamadas = []

    async def respaldar():
        llamadas.append(1)
        return {}
    monkeypatch.setattr(respaldo, "respaldar_y_limpiar", respaldar)
    monkeypatch.setattr(horario, "ahora_chile", lambda: datetime(2026, 10, 15, 6, 30, tzinfo=CL))
    assert await tareas.diarias(None) is None and not llamadas
    monkeypatch.setattr(horario, "ahora_chile", lambda: datetime(2026, 10, 15, 7, 5, tzinfo=CL))
    hecho = await tareas.diarias(None)
    assert hecho == date(2026, 10, 15) and await tareas.diarias(hecho) == hecho and len(llamadas) == 1


def test_columna_nueva_se_agrega_sola_en_tablas_existentes():
    from sqlalchemy import create_engine, inspect, text

    from agent.db import _migrar

    eng = create_engine("sqlite://")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE usp_comprobantes (id INTEGER PRIMARY KEY, telefono VARCHAR(50))"))
        _migrar(conn)
        _migrar(conn)  # se puede repetir
        assert "respaldado_en" in {c["name"] for c in inspect(conn).get_columns("usp_comprobantes")}


async def test_panel_prueba_de_respaldo(db, proveedor, monkeypatch):
    from agent import respaldo
    from tests.test_agente import H

    async def enviar(asunto, html, adjuntos):
        return (asunto.startswith("Prueba") and adjuntos == []), "ok"
    monkeypatch.setattr(respaldo, "_enviar", enviar)
    async with await _cliente(proveedor) as c:
        r = await c.post("/admin/api/respaldo/prueba", headers=H)
        assert r.json() == {"ok": True, "detalle": "ok"}
        assert (await c.post("/admin/api/respaldo", headers=H)).json()["respaldados"] == 0
        assert (await c.post("/admin/api/respaldo/prueba")).status_code == 401
