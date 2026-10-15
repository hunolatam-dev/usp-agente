-- Casos de WhatsApp USP en app.huno.cl (Finanzas)
-- Ejecutar en Supabase -> SQL Editor DESPUES de supabase_usp.sql. Se puede repetir.
--
-- Solo el personal con el modulo Finanzas (personal.modulos_acceso contiene "finanzas")
-- o administrador puede ver y resolver casos. La sesion se valida con huno_sesion_pid,
-- igual que el resto de app.huno.cl. Las tablas usp_* siguen cerradas (RLS sin politicas):
-- el acceso es SOLO a traves de estas funciones.

begin;

-- Devuelve la ficha de la persona si su sesion es valida y tiene Finanzas; si no, error.
create or replace function public._usp_persona_finanzas(p_token text)
returns jsonb
language plpgsql stable security definer
set search_path to 'public', 'extensions'
as $$
declare
  v_pid bigint;
  v_p jsonb;
  v_mod text;
  v_roles text;
begin
  v_pid := public.huno_sesion_pid(p_token);
  if v_pid is null then
    raise exception 'Sin sesión válida' using errcode = '28000';
  end if;
  select to_jsonb(p) into v_p from public.personal p where p.id = v_pid;
  v_mod := lower(coalesce(v_p ->> 'modulos_acceso', ''));
  v_roles := lower(coalesce(v_p ->> 'roles', '') || ' ' || coalesce(v_p ->> 'rol', ''));
  if not (
       coalesce((v_p ->> 'es_admin')::boolean, false)
    or v_mod like '%finanzas%'
    or v_roles like '%admin%'
    or (v_mod in ('', '[]', '{}') and v_roles like '%finanzas%')
  ) then
    raise exception 'Requiere el módulo Finanzas' using errcode = '42501';
  end if;
  return v_p;
end $$;

-- Lista de casos (abiertos por defecto), con el comprobante asociado (sin el archivo)
create or replace function public.huno_usp_casos_listar(p_token text, p_estado text default 'abierto')
returns jsonb
language plpgsql security definer
set search_path to 'public'
as $$
begin
  perform public._usp_persona_finanzas(p_token);
  return coalesce((
    select jsonb_agg(x order by x.creado_en desc)
    from (
      select e.id, e.telefono, e.motivo, e.resumen, e.estado, e.creado_en,
             e.resuelto_en, e.resuelto_por, e.nota_resolucion,
             cv.nombre_whatsapp,
             (select coalesce(jsonb_agg(jsonb_build_object(
                        'id', a.id, 'creado_en', a.creado_en, 'mime', a.mime, 'monto', a.monto,
                        'estado', a.estado, 'es_comprobante', a.es_comprobante) order by a.creado_en desc), '[]'::jsonb)
                from (select * from public.usp_comprobantes a
                       where a.telefono = e.telefono and a.archivo is not null
                       order by a.creado_en desc limit 20) a) as archivos,
             case when c.id is null then null else jsonb_build_object(
               'id', c.id, 'monto', c.monto, 'fecha', c.fecha, 'numero_operacion', c.numero_operacion,
               'banco_origen', c.banco_origen, 'rut_origen', c.rut_origen, 'nombre_origen', c.nombre_origen,
               'cuenta_destino', c.cuenta_destino, 'estado', c.estado, 'tiene_archivo', c.archivo is not null
             ) end as comprobante
      from public.usp_escalamientos e
      left join public.usp_comprobantes c on c.id = e.comprobante_id
      left join public.usp_conversaciones cv on cv.telefono = e.telefono
      where e.estado = coalesce(nullif(p_estado, ''), 'abierto')
      order by e.creado_en desc
      limit 300
    ) x
  ), '[]'::jsonb);
end $$;

-- Marca un caso como resuelto, con el nombre de quien lo hizo
create or replace function public.huno_usp_caso_resolver(p_token text, p_id bigint, p_nota text default null)
returns jsonb
language plpgsql security definer
set search_path to 'public'
as $$
declare
  v_p jsonb;
  v_quien text;
  v_e public.usp_escalamientos;
begin
  v_p := public._usp_persona_finanzas(p_token);
  v_quien := left(trim(coalesce(v_p ->> 'nombre', '') || ' ' || coalesce(v_p ->> 'apellido', '')), 100);
  update public.usp_escalamientos
     set estado = 'resuelto', resuelto_en = now(),
         resuelto_por = coalesce(nullif(v_quien, ''), 'app.huno.cl'),
         nota_resolucion = nullif(left(p_nota, 4000), '')
   where id = p_id and estado = 'abierto'
  returning * into v_e;
  if v_e.id is null then
    raise exception 'El caso no existe o ya estaba resuelto';
  end if;
  return to_jsonb(v_e);
end $$;

-- Archivo del comprobante de un caso (imagen o PDF) en base64
create or replace function public.huno_usp_caso_comprobante(p_token text, p_id bigint)
returns jsonb
language plpgsql security definer
set search_path to 'public'
as $$
declare r jsonb;
begin
  perform public._usp_persona_finanzas(p_token);
  select jsonb_build_object('mime', c.mime, 'base64', encode(c.archivo, 'base64'))
    into r
  from public.usp_escalamientos e
  join public.usp_comprobantes c on c.id = e.comprobante_id
  where e.id = p_id and c.archivo is not null;
  return r;
end $$;

-- Cualquier archivo recibido por WhatsApp (imagen o PDF), por su id, en base64
create or replace function public.huno_usp_archivo(p_token text, p_id bigint)
returns jsonb
language plpgsql security definer
set search_path to 'public'
as $$
declare r jsonb;
begin
  perform public._usp_persona_finanzas(p_token);
  select jsonb_build_object('id', c.id, 'mime', c.mime, 'telefono', c.telefono, 'creado_en', c.creado_en,
                            'base64', encode(c.archivo, 'base64'))
    into r
  from public.usp_comprobantes c
  where c.id = p_id and c.archivo is not null;
  return r;
end $$;

-- Todos los archivos recibidos (sin el contenido), los mas recientes primero
create or replace function public.huno_usp_archivos_listar(p_token text, p_limite integer default 200)
returns jsonb
language plpgsql security definer
set search_path to 'public'
as $$
begin
  perform public._usp_persona_finanzas(p_token);
  return coalesce((
    select jsonb_agg(x order by x.creado_en desc)
    from (
      select c.id, c.telefono, c.creado_en, c.mime, c.monto, c.fecha, c.numero_operacion,
             c.nombre_origen, c.estado, c.motivo, c.es_comprobante, cv.nombre_whatsapp,
             octet_length(c.archivo) as bytes
      from public.usp_comprobantes c
      left join public.usp_conversaciones cv on cv.telefono = c.telefono
      where c.archivo is not null
      order by c.creado_en desc
      limit least(greatest(coalesce(p_limite, 200), 1), 1000)
    ) x
  ), '[]'::jsonb);
end $$;

revoke all on function public._usp_persona_finanzas(text) from public, anon, authenticated;
revoke all on function public.huno_usp_casos_listar(text, text) from public;
revoke all on function public.huno_usp_caso_resolver(text, bigint, text) from public;
revoke all on function public.huno_usp_caso_comprobante(text, bigint) from public;
grant execute on function public.huno_usp_casos_listar(text, text) to anon, authenticated;
grant execute on function public.huno_usp_caso_resolver(text, bigint, text) to anon, authenticated;
grant execute on function public.huno_usp_caso_comprobante(text, bigint) to anon, authenticated;
revoke all on function public.huno_usp_archivo(text, bigint) from public;
revoke all on function public.huno_usp_archivos_listar(text, integer) from public;
grant execute on function public.huno_usp_archivo(text, bigint) to anon, authenticated;
grant execute on function public.huno_usp_archivos_listar(text, integer) to anon, authenticated;

commit;
