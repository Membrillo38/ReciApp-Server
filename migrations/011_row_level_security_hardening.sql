-- Restore FORCE RLS for the API role without replacing existing business
-- routines whose definitions may differ across self-hosted deployments.
-- This policy-only migration can be reapplied safely.

create or replace function public.app_is_service()
returns boolean
language sql
stable
parallel safe
set search_path = pg_catalog, public
as $$
  select coalesce(current_setting('app.actor', true), '') = 'service'
$$;

create or replace function public.app_is_auth()
returns boolean
language sql
stable
parallel safe
set search_path = pg_catalog, public
as $$
  select coalesce(current_setting('app.actor', true), '') = 'auth'
$$;

create or replace function public.app_user_id()
returns uuid
language sql
stable
parallel safe
set search_path = pg_catalog, public
as $$
  select nullif(current_setting('app.user_id', true), '')::uuid
$$;

revoke all on function public.app_is_service() from public;
revoke all on function public.app_is_auth() from public;
revoke all on function public.app_user_id() from public;

do $$
begin
  if exists (select 1 from pg_catalog.pg_roles where rolname = 'reciapp') then
    grant execute on function public.app_is_service() to reciapp;
    grant execute on function public.app_is_auth() to reciapp;
    grant execute on function public.app_user_id() to reciapp;
  end if;
end;
$$;

do $$
declare
  table_name text;
begin
  foreach table_name in array array[
    'profiles',
    'recipes',
    'user_recipes',
    'extract_jobs',
    'usage_events',
    'api_request_logs',
    'app_settings',
    'subscription_events',
    'api_spend_ledger',
    'security_events',
    'spend_alerts',
    'extract_job_access',
    'apple_notification_events',
    'recipe_translations',
    'auth_refresh_tokens'
  ]
  loop
    execute format('alter table public.%I enable row level security', table_name);
    execute format('alter table public.%I force row level security', table_name);
    execute format('drop policy if exists reciapp_service on public.%I', table_name);
    execute format(
      'create policy reciapp_service on public.%I using (public.app_is_service()) with check (public.app_is_service())',
      table_name
    );
  end loop;
end;
$$;

drop policy if exists profiles_auth on public.profiles;
create policy profiles_auth on public.profiles
  using (public.app_is_auth())
  with check (public.app_is_auth());
drop policy if exists profiles_self on public.profiles;
create policy profiles_self on public.profiles
  using (id = public.app_user_id())
  with check (id = public.app_user_id());

drop policy if exists recipes_user_select on public.recipes;
create policy recipes_user_select on public.recipes
  for select using (public.app_user_id() is not null);
drop policy if exists recipes_user_insert on public.recipes;
create policy recipes_user_insert on public.recipes
  for insert with check (public.app_user_id() is not null);
drop policy if exists recipes_user_update on public.recipes;
create policy recipes_user_update on public.recipes
  for update using (public.app_user_id() is not null)
  with check (public.app_user_id() is not null);

drop policy if exists recipe_translations_user_select on public.recipe_translations;
create policy recipe_translations_user_select on public.recipe_translations
  for select using (public.app_user_id() is not null);
drop policy if exists recipe_translations_user_insert on public.recipe_translations;
create policy recipe_translations_user_insert on public.recipe_translations
  for insert with check (public.app_user_id() is not null);
drop policy if exists recipe_translations_user_update on public.recipe_translations;
create policy recipe_translations_user_update on public.recipe_translations
  for update using (public.app_user_id() is not null)
  with check (public.app_user_id() is not null);

drop policy if exists user_recipes_self on public.user_recipes;
create policy user_recipes_self on public.user_recipes
  using (user_id = public.app_user_id())
  with check (user_id = public.app_user_id());

drop policy if exists extract_jobs_select on public.extract_jobs;
create policy extract_jobs_select on public.extract_jobs
  for select using (
    user_id = public.app_user_id()
    or exists (
      select 1 from public.extract_job_access access
       where access.job_id = extract_jobs.id
         and access.user_id = public.app_user_id()
    )
  );
drop policy if exists extract_jobs_insert on public.extract_jobs;
create policy extract_jobs_insert on public.extract_jobs
  for insert with check (user_id = public.app_user_id());
drop policy if exists extract_jobs_update on public.extract_jobs;
create policy extract_jobs_update on public.extract_jobs
  for update using (
    user_id = public.app_user_id()
    or exists (
      select 1 from public.extract_job_access access
       where access.job_id = extract_jobs.id
         and access.user_id = public.app_user_id()
    )
  )
  with check (
    user_id = public.app_user_id()
    or exists (
      select 1 from public.extract_job_access access
       where access.job_id = extract_jobs.id
         and access.user_id = public.app_user_id()
    )
  );

drop policy if exists extract_job_access_self on public.extract_job_access;
create policy extract_job_access_self on public.extract_job_access
  using (user_id = public.app_user_id())
  with check (user_id = public.app_user_id());

drop policy if exists usage_events_self on public.usage_events;
create policy usage_events_self on public.usage_events
  using (user_id = public.app_user_id())
  with check (user_id = public.app_user_id());

drop policy if exists api_spend_ledger_self on public.api_spend_ledger;
create policy api_spend_ledger_self on public.api_spend_ledger
  using (user_id = public.app_user_id())
  with check (user_id = public.app_user_id());

drop policy if exists auth_refresh_tokens_auth on public.auth_refresh_tokens;
create policy auth_refresh_tokens_auth on public.auth_refresh_tokens
  using (public.app_is_auth())
  with check (public.app_is_auth());
drop policy if exists auth_refresh_tokens_self on public.auth_refresh_tokens;
create policy auth_refresh_tokens_self on public.auth_refresh_tokens
  using (user_id = public.app_user_id())
  with check (user_id = public.app_user_id());

drop policy if exists app_settings_read on public.app_settings;
create policy app_settings_read on public.app_settings
  for select using (public.app_user_id() is not null or public.app_is_auth());

drop policy if exists api_request_logs_insert on public.api_request_logs;
create policy api_request_logs_insert on public.api_request_logs
  for insert with check (true);

drop policy if exists security_events_insert on public.security_events;
create policy security_events_insert on public.security_events
  for insert with check (true);
drop policy if exists security_events_self_update on public.security_events;
create policy security_events_self_update on public.security_events
  for update using (user_id = public.app_user_id())
  with check (user_id = public.app_user_id() or user_id is null);
