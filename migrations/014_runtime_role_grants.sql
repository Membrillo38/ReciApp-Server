-- Make the least-privilege API/worker role reproducible on fresh databases.
-- FORCE RLS remains the row-level boundary; this role must never bypass it.

do $$
declare
  runtime_role record;
begin
  select rolsuper, rolbypassrls, rolcanlogin
    into runtime_role
    from pg_catalog.pg_roles
   where rolname = 'reciapp_runtime';

  if not found then
    raise exception 'Create reciapp_runtime before applying migration 014';
  end if;
  if runtime_role.rolsuper or runtime_role.rolbypassrls or not runtime_role.rolcanlogin then
    raise exception 'reciapp_runtime must be LOGIN, NOSUPERUSER, and NOBYPASSRLS';
  end if;
end;
$$;

grant usage on schema public to reciapp_runtime;
grant all privileges on all tables in schema public to reciapp_runtime;
grant all privileges on all sequences in schema public to reciapp_runtime;

grant execute on function public.app_is_service() to reciapp_runtime;
grant execute on function public.app_is_auth() to reciapp_runtime;
grant execute on function public.app_user_id() to reciapp_runtime;
grant execute on function public.reserve_api_spend(uuid, uuid, numeric, numeric, numeric, numeric) to reciapp_runtime;
grant execute on function public.settle_api_spend(uuid, numeric, text) to reciapp_runtime;
grant execute on function public.claim_next_extract_job(integer) to reciapp_runtime;
grant execute on function public.unregister_push_device_for_refresh(text, text) to reciapp_runtime;
