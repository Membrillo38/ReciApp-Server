-- Persist the request key and successor hash so a lost refresh response can
-- be retried once with the same old token and request ID.
alter table public.auth_refresh_tokens
  add column if not exists rotation_request_id uuid,
  add column if not exists rotation_retry_until timestamptz,
  add column if not exists rotated_token_hash text;
