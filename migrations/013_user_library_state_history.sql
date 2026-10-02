-- Keep recent server snapshots so explicit conflict resolution can always be undone.
-- Additive and safe to leave in place if the API rolls back.

alter table public.user_library_state
  add column if not exists history jsonb not null default '[]'::jsonb;

do $$
begin
  if not exists (
    select 1 from pg_constraint
     where conname = 'user_library_state_history_is_array'
       and conrelid = 'public.user_library_state'::regclass
  ) then
    alter table public.user_library_state
      add constraint user_library_state_history_is_array
      check (jsonb_typeof(history) = 'array');
  end if;
end;
$$;

alter table public.user_library_state enable row level security;
alter table public.user_library_state force row level security;
