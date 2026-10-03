-- Per-account client library metadata, kept separately from recipe content.
-- Additive: old API builds do not read or write this table; dropping it would
-- discard user data, so rollback is a code rollback only.

create table if not exists public.user_library_state (
  user_id uuid primary key references public.profiles (id) on delete cascade,
  revision bigint not null default 1 check (revision > 0),
  snapshot jsonb not null default '{"schema_version":1,"folders":null,"shopping_list":[],"preferences":null,"cooking_progress":{}}'::jsonb
    check (jsonb_typeof(snapshot) = 'object'),
  updated_at timestamptz not null default now()
);

alter table public.user_library_state enable row level security;
alter table public.user_library_state force row level security;
drop policy if exists user_library_state_service on public.user_library_state;
create policy user_library_state_service on public.user_library_state
  using (public.app_is_service())
  with check (public.app_is_service());

do $$
begin
  if exists (select 1 from pg_catalog.pg_roles where rolname = 'reciapp') then
    grant all privileges on table public.user_library_state to reciapp;
  end if;
  if exists (select 1 from pg_catalog.pg_roles where rolname = 'reciapp_runtime') then
    grant all privileges on table public.user_library_state to reciapp_runtime;
  end if;
end;
$$;
