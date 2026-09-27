-- Durable, per-device recipe-completion notifications.
-- Additive migration. Keeping these tables after code rollback is safe.

create table public.push_devices (
  token_hash text primary key check (token_hash ~ '^[0-9a-f]{64}$'),
  user_id uuid not null references public.profiles (id) on delete cascade,
  device_token text not null check (
    char_length(device_token) between 32 and 512
    and device_token ~ '^[0-9A-Fa-f]+$'
  ),
  environment text not null check (environment in ('sandbox', 'production')),
  language_code text not null default 'en-US',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index push_devices_user_idx on public.push_devices (user_id);

create table public.push_deliveries (
  id uuid primary key default gen_random_uuid(),
  job_id uuid not null references public.extract_jobs (id) on delete cascade,
  user_id uuid not null references public.profiles (id) on delete cascade,
  token_hash text not null check (token_hash ~ '^[0-9a-f]{64}$'),
  device_token text,
  environment text not null check (environment in ('sandbox', 'production')),
  status text not null default 'pending' check (status in ('pending', 'processing', 'sent', 'failed')),
  attempts smallint not null default 0 check (attempts >= 0 and attempts <= 8),
  next_attempt_at timestamptz not null default now(),
  lease_until timestamptz,
  sent_at timestamptz,
  last_error_code text,
  created_at timestamptz not null default now(),
  unique (job_id, token_hash)
);

create index push_deliveries_claim_idx
  on public.push_deliveries (status, next_attempt_at, created_at)
  where status in ('pending', 'processing');
create index push_deliveries_user_idx on public.push_deliveries (user_id, created_at desc);

create table public.app_runtime_heartbeats (
  service_name text primary key check (service_name in ('recipe-worker')),
  last_seen_at timestamptz not null default now()
);
alter table public.app_runtime_heartbeats enable row level security;
alter table public.app_runtime_heartbeats force row level security;
create policy app_runtime_heartbeats_service on public.app_runtime_heartbeats
  using (public.app_is_service()) with check (public.app_is_service());

alter table public.push_devices enable row level security;
alter table public.push_devices force row level security;
create policy push_devices_service on public.push_devices
  using (public.app_is_service()) with check (public.app_is_service());
create policy push_devices_self on public.push_devices
  using (user_id = public.app_user_id()) with check (user_id = public.app_user_id());

alter table public.push_deliveries enable row level security;
alter table public.push_deliveries force row level security;
create policy push_deliveries_service on public.push_deliveries
  using (public.app_is_service()) with check (public.app_is_service());
create policy push_deliveries_delete_self on public.push_deliveries
  for delete using (user_id = public.app_user_id());

do $$
begin
  if exists (select 1 from pg_catalog.pg_roles where rolname = 'reciapp') then
    grant all privileges on table public.push_devices to reciapp;
    grant all privileges on table public.push_deliveries to reciapp;
    grant all privileges on table public.app_runtime_heartbeats to reciapp;
  end if;
end;
$$;

create or replace function public.enqueue_recipe_completion_pushes()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  previous_actor text := coalesce(current_setting('app.actor', true), '');
  previous_user text := coalesce(current_setting('app.user_id', true), '');
begin
  if new.status <> 'completed'
     or new.job_kind not in ('extract', 'translation')
     or new.cache_hit
     or new.recipe_id is null
     or new.user_id is null
     or (tg_op = 'UPDATE' and old.status = 'completed') then
    return new;
  end if;

  perform set_config('app.actor', 'service', true);
  insert into public.push_deliveries (
    job_id, user_id, token_hash, device_token, environment
  )
  select new.id, device.user_id, device.token_hash, device.device_token, device.environment
    from public.push_devices device
    join public.profiles profile on profile.id = device.user_id and profile.deleted_at is null
   where (
     device.user_id = new.user_id
     or exists (
       select 1 from public.extract_job_access access
        where access.job_id = new.id and access.user_id = device.user_id
     )
   )
   for key share of device
  on conflict (job_id, token_hash) do nothing;
  perform set_config('app.actor', previous_actor, true);
  perform set_config('app.user_id', previous_user, true);
  return new;
end;
$$;

revoke all on function public.enqueue_recipe_completion_pushes() from public;

create trigger extract_job_enqueue_recipe_completion_pushes
  after insert or update of status on public.extract_jobs
  for each row execute function public.enqueue_recipe_completion_pushes();

-- Logout can remove one device registration atomically with refresh revocation.
create or replace function public.unregister_push_device_for_refresh(
  p_refresh_token_hash text,
  p_device_token_hash text
)
returns void
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_user_id uuid;
  previous_actor text := coalesce(current_setting('app.actor', true), '');
  previous_user text := coalesce(current_setting('app.user_id', true), '');
begin
  select user_id into v_user_id
    from public.auth_refresh_tokens
   where token_hash = p_refresh_token_hash
   limit 1;
  if v_user_id is null then
    return;
  end if;
  perform set_config('app.actor', 'service', true);
  delete from public.push_deliveries
   where user_id = v_user_id and token_hash = p_device_token_hash;
  delete from public.push_devices
   where user_id = v_user_id and token_hash = p_device_token_hash;
  perform set_config('app.actor', previous_actor, true);
  perform set_config('app.user_id', previous_user, true);
end;
$$;

revoke all on function public.unregister_push_device_for_refresh(text, text) from public;

do $$
begin
  if exists (select 1 from pg_catalog.pg_roles where rolname = 'reciapp') then
    grant execute on function public.unregister_push_device_for_refresh(text, text) to reciapp;
  end if;
end;
$$;
