"""Registro del numero de WhatsApp por el propio cliente (RUT + correo registrado + codigo del Estado de cuenta)."""

import json
import os

import httpx
import pytest
from sqlalchemy import select, text

from agent import comprobantes, flujo, herramientas, huno, llm, registro
from agent.db import ClienteHuno, Escalamiento, async_session, engine
from agent.providers.base import MensajeEntrante
from tests.test_agente import TEL_DESCONOCIDO, TEL_MARIA, cargar_ejemplos, contar, datos, mensaje

CORREO = "pagos@copayapu.cl"


@pytest.fixture
async def base(db, monkeypatch):
    await cargar_ejemplos()
    async with async_session() as s:   # Transportes Copayapu (76.999.888-8) no tiene telefonos, si correos
        c = await s.get(ClienteHuno, 3)
        c.correos = json.dumps([{"valor": "contacto@copayapu.cl", "label": ""}, {"valor": CORREO, "label": "Pagos"}])
        await s.commit()
    huno.invalidar_cache()

    async def no_llamar(**kw):
        raise AssertionError("el registro no debe gastar tokens")
    monkeypatch.setattr(llm, "crear_mensaje", no_llamar)
    enviados = []

    async def enviar(rut):
        enviados.append(rut)
        return True, "co•••••••@copayapu.cl"

    async def verificar(rut, codigo):
        return {"ok": True, "nombre": "x"} if codigo == "123456" else {"ok": False, "error": "codigo", "restantes": 3}
    monkeypatch.setattr(registro, "enviar_codigo", enviar)
    monkeypatch.setattr(registro, "verificar_codigo", verificar)
    return enviados


async def decir(proveedor, texto, tel=TEL_DESCONOCIDO, n=[0]):
    n[0] += 1
    r, _ = await flujo.procesar(proveedor, mensaje(tel, texto, evento=f"reg{n[0]}"))
    return "\n\n".join(r)


async def test_registro_completo_por_whatsapp(base, proveedor):
    r = await decir(proveedor, "hola")
    assert "no hemos podido validar este número" in r and "RUT de la empresa" in r
    assert "No hemos podido validar su número" in await decir(proveedor, "cuánto debo?")
    assert "correo" in await decir(proveedor, "76.999.888-8")
    assert "Le quedan 2 intento(s)" in await decir(proveedor, "otro@copayapu.cl")
    assert "nombre y apellido" in await decir(proveedor, "RUT 76.999.888-8, correo PAGOS@Copayapu.cl")
    r = await decir(proveedor, "me llamo pedro gonzález")
    assert "Gracias, Pedro" in r and "co•••••••@copayapu.cl" in r and "Estado de cuenta" in r
    assert base == ["76999888-8"]
    assert "Le quedan 3 intento(s)" in await decir(proveedor, "111111")
    r = await decir(proveedor, "123 456")
    assert r.startswith("Listo, Pedro. Su número quedó registrado en Transportes Copayapu SpA")

    async with async_session() as s:
        fila = await s.get(ClienteHuno, 3)
        caso = (await s.execute(select(Escalamiento).where(Escalamiento.motivo == "registro_whatsapp"))).scalars().one()
    assert json.loads(fila.telefonos) == [{"valor": "+" + TEL_DESCONOCIDO, "label": "Pedro González"}]
    assert caso.estado == "resuelto" and CORREO in caso.resumen
    # Desde ahora se le habla por su nombre y con las facturas de su empresa
    assert (await decir(proveedor, "hola")).startswith("Hola Pedro,")
    f = await herramientas.consultar_facturas_pendientes(TEL_DESCONOCIDO)
    assert f["empresas"][0]["empresa"] == "Transportes Copayapu SpA"
    assert await registro.estado(TEL_DESCONOCIDO) == {"fallos": [], "envios": []}


async def test_numero_antiguo_en_texto_libre_se_conserva(base, proveedor):
    """Comercial Atacama tiene el telefono guardado como texto; al registrar otro se pasa al formato de la app."""
    async with async_session() as s:
        c = await s.get(ClienteHuno, 1)
        c.correos = json.dumps([{"valor": "maria@atacama.cl", "label": ""}])
        await s.commit()
    huno.invalidar_cache()
    await decir(proveedor, "76.123.456-0 maria@atacama.cl")
    await decir(proveedor, "Luis Rojas")
    await decir(proveedor, "123456")
    tels = huno.parsear_telefonos((await _fila(1)).telefonos)
    assert tels == [(TEL_MARIA, "María Pérez"), (TEL_DESCONOCIDO, "Luis Rojas")]


async def _fila(cid):
    async with async_session() as s:
        return await s.get(ClienteHuno, cid)


async def test_tres_intentos_fallidos_derivan(base, proveedor):
    for correo in ("a@x.cl", "b@x.cl"):
        assert "No pudimos validar" in await decir(proveedor, f"76.999.888-8 {correo}")
    r = await decir(proveedor, "76.999.888-8 c@x.cl")
    assert "ejecutivo" in r
    assert await contar(Escalamiento, Escalamiento.motivo == "registro_contacto") == 1
    # Ni con los datos correctos sigue, hasta que lo vea una persona
    assert "ejecutivo" in await decir(proveedor, f"76.999.888-8 {CORREO}")


async def test_nuevo_codigo_y_limite_de_envios(base, proveedor):
    await decir(proveedor, f"76.999.888-8 {CORREO}")
    await decir(proveedor, "Ana Díaz")
    assert "Le enviamos" in await decir(proveedor, "no me llegó, me manda otro código?")
    assert "Le enviamos" in await decir(proveedor, "nuevo código")
    assert "ejecutivo" in await decir(proveedor, "nuevo código")
    assert len(base) == 3


async def test_cancelar_y_nombre_invalido(base, proveedor):
    await decir(proveedor, f"76.999.888-8 {CORREO}")
    assert "No logré leer su nombre" in await decir(proveedor, "12345")
    assert "No logré leer su nombre" in await decir(proveedor, "hola")
    assert registro.limpiar_nombre("Soy MARÍA JOSÉ de la Fuente") == "María José de la Fuente"
    assert "dejé el registro sin terminar" in await decir(proveedor, "cancelar")
    assert (await registro.estado(TEL_DESCONOCIDO)).get("paso") is None


async def test_comprobante_en_espera_se_resuelve_con_el_registro(base, proveedor, monkeypatch):
    async def leer(contenido, mime):
        return datos(690000, fecha=__import__("datetime").date(2026, 10, 12))
    monkeypatch.setattr(comprobantes, "leer", leer)
    proveedor.archivos["u"] = (b"\xff\xd8img", "image/jpeg")
    r, _ = await flujo.procesar(proveedor, mensaje(TEL_DESCONOCIDO, url="u", evento="cmp"))
    assert "RUT de la empresa" in r[0]
    r = await decir(proveedor, f"76.999.888-8 {CORREO}")
    assert "Pago confirmado" in r and "nombre y apellido" in r


async def test_numero_registrado_no_entra_al_registro_sin_pedirlo(base, proveedor):
    async with async_session() as s:
        c = await s.get(ClienteHuno, 1)
        c.correos = json.dumps([{"valor": "maria@atacama.cl"}])
        await s.commit()
    assert await registro.manejar(TEL_MARIA, f"76.999.888-8 {CORREO}", registrado=True) is None
    # Pero si lo pide (la IA usa la herramienta), puede agregar otra empresa
    r = json.loads(await herramientas.ejecutar("registrar_telefono", {"rut": "76.999.888-8", "correo": CORREO}, TEL_MARIA))
    assert "nombre y apellido" in r["mensaje_para_el_cliente"]


async def test_contacto_sin_numero_visible_se_deriva(base, proveedor):
    bsuid = "CL.1234567890ABC"
    r, _ = await flujo.procesar(proveedor, MensajeEntrante(telefono=bsuid, texto=f"76.999.888-8 {CORREO}", mensaje_id="b",
                                                          contexto={"conversation_id": "c-b"}))
    assert "ejecutivo" in r[0]


async def test_envio_del_codigo_usa_la_funcion_del_estado_de_cuenta(db, monkeypatch):
    vistos = []

    def manejar(req):
        vistos.append(req)
        return httpx.Response(200, json={"ok": True, "email_mask": "pa•••@copayapu.cl", "expira": 1})
    real = httpx.AsyncClient
    monkeypatch.setattr(registro.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(manejar)))
    assert await registro.enviar_codigo("76999888-8") == (True, "pa•••@copayapu.cl")
    req = vistos[0]
    assert str(req.url).endswith("/functions/v1/send-otp") and json.loads(req.content) == {"rut": "76.999.888-8"}
    assert req.headers["apikey"].startswith("eyJ") and req.headers["authorization"].startswith("Bearer eyJ")


@pytest.mark.skipif(not (os.getenv("TEST_DATABASE_URL") or "").startswith("postgres"), reason="solo Postgres")
async def test_verificar_codigo_llama_a_ec_verificar(db):
    async with engine.begin() as c:
        await c.execute(text("create table if not exists public.ec_sesion (hash text primary key, cliente_id bigint, "
                             "expira timestamptz, creado_en timestamptz default now())"))
        await c.execute(text("""
            create or replace function public.ec_verificar(p_rut text, p_codigo text) returns jsonb language plpgsql as $$
            begin
              if p_codigo <> '123456' then return jsonb_build_object('ok', false, 'error', 'codigo', 'restantes', 4); end if;
              insert into public.ec_sesion (hash, cliente_id, expira)
                values (encode(sha256(convert_to('tok' || p_rut, 'UTF8')), 'hex'), 3, now() + interval '20 minutes');
              return jsonb_build_object('ok', true, 'token', 'tok' || p_rut, 'nombre', 'X');
            end $$"""))
    assert await registro.verificar_codigo("76999888-8", "000000") == {"ok": False, "error": "codigo", "restantes": 4}
    r = await registro.verificar_codigo("76999888-8", "123456")
    assert r == {"ok": True, "nombre": "X"}
    async with engine.connect() as c:
        assert (await c.execute(text("select count(*) from public.ec_sesion"))).scalar() == 0  # la sesion web se cierra
