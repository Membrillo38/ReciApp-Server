from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock
from typing import Any, Iterable, Iterator

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from starlette.concurrency import run_in_threadpool

from app.config import settings


_pool: ConnectionPool | None = None
_pool_lock = Lock()
_db_actor: ContextVar[str] = ContextVar("reciapp_db_actor", default="")
_db_user_id: ContextVar[str] = ContextVar("reciapp_db_user_id", default="")


def _adapt(value: Any) -> Any:
    if isinstance(value, dict):
        return Jsonb(value)
    return value


def _params(params: Iterable[Any] | dict[str, Any] | None) -> Iterable[Any] | dict[str, Any] | None:
    if params is None:
        return None
    if isinstance(params, dict):
        return {key: _adapt(value) for key, value in params.items()}
    return tuple(_adapt(value) for value in params)


def get_pool() -> ConnectionPool:
    global _pool
    with _pool_lock:
        if _pool is None:
            settings.validate_database()
            _pool = ConnectionPool(
                settings.database_url,
                min_size=1,
                max_size=10,
                open=True,
                kwargs={"row_factory": dict_row},
            )
        return _pool


@contextmanager
def db_context(*, actor: str, user_id: str = "") -> Iterator[None]:
    actor_token = _db_actor.set(actor)
    user_token = _db_user_id.set(user_id)
    try:
        yield
    finally:
        _db_actor.reset(actor_token)
        _db_user_id.reset(user_token)


def recipe_worker_is_healthy() -> bool:
    if not settings.worker_enabled:
        return False
    with db_context(actor="service"):
        row = fetch_one(
            """
            select exists (
                select 1 from app_runtime_heartbeats
                 where service_name = 'recipe-worker'
                   and last_seen_at > now() - (%s * interval '1 second')
            ) as healthy
            """,
            (settings.worker_heartbeat_ttl_seconds,),
        )
    return bool(row and row.get("healthy"))


def db_context_for_request(path: str, user_id: str | None) -> tuple[str, str]:
    """Map an HTTP path to RLS GUC values. Empty actor is fail-closed."""
    if path.startswith("/dashboard") or path.startswith("/v1/admin") or path.startswith("/v1/webhooks"):
        return "service", ""
    if path.startswith("/v1/auth/"):
        return "auth", user_id or ""
    if user_id:
        return "user", user_id
    return "", ""


@contextmanager
def get_conn():
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select set_config('app.actor', %s, true), set_config('app.user_id', %s, true)",
                (_db_actor.get(), _db_user_id.get()),
            )
        yield conn


def reset_db() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
        _pool = None


def fetch_one(sql: str, params: Iterable[Any] | dict[str, Any] | None = None) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, _params(params))
            row = cur.fetchone()
            return dict(row) if row else None


def fetch_all(sql: str, params: Iterable[Any] | dict[str, Any] | None = None) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, _params(params))
            return [dict(row) for row in cur.fetchall()]


def execute(sql: str, params: Iterable[Any] | dict[str, Any] | None = None) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, _params(params))
            return cur.rowcount


def execute_returning(sql: str, params: Iterable[Any] | dict[str, Any] | None = None) -> dict | None:
    return fetch_one(sql, params)


async def probe_postgres() -> None:
    heartbeat_ttl = int(settings.worker_heartbeat_ttl_seconds)
    query = f"""
        select
          exists (
            select 1 from pg_roles
             where rolname = current_user
               and (rolsuper or rolbypassrls)
          ) as runtime_role_bypasses_rls,
          (
            has_schema_privilege(current_user, 'public', 'USAGE')
            and not exists (
              select 1
                from pg_class c
                join pg_namespace n on n.oid = c.relnamespace
               where n.nspname = 'public'
                 and c.relkind in ('r', 'p')
                 and not (
                   has_table_privilege(current_user, c.oid, 'SELECT')
                   and has_table_privilege(current_user, c.oid, 'INSERT')
                   and has_table_privilege(current_user, c.oid, 'UPDATE')
                   and has_table_privilege(current_user, c.oid, 'DELETE')
                 )
            )
            and not exists (
              select 1
                from pg_class c
                join pg_namespace n on n.oid = c.relnamespace
               where n.nspname = 'public'
                 and c.relkind = 'S'
                 and not (
                   has_sequence_privilege(current_user, c.oid, 'USAGE')
                   and has_sequence_privilege(current_user, c.oid, 'SELECT')
                   and has_sequence_privilege(current_user, c.oid, 'UPDATE')
                 )
            )
            and to_regprocedure('public.app_is_service()') is not null
            and has_function_privilege(current_user, to_regprocedure('public.app_is_service()'), 'EXECUTE')
            and to_regprocedure('public.app_is_auth()') is not null
            and has_function_privilege(current_user, to_regprocedure('public.app_is_auth()'), 'EXECUTE')
            and to_regprocedure('public.app_user_id()') is not null
            and has_function_privilege(current_user, to_regprocedure('public.app_user_id()'), 'EXECUTE')
            and to_regprocedure('public.reserve_api_spend(uuid,uuid,numeric,numeric,numeric,numeric)') is not null
            and has_function_privilege(current_user, to_regprocedure('public.reserve_api_spend(uuid,uuid,numeric,numeric,numeric,numeric)'), 'EXECUTE')
            and to_regprocedure('public.settle_api_spend(uuid,numeric,text)') is not null
            and has_function_privilege(current_user, to_regprocedure('public.settle_api_spend(uuid,numeric,text)'), 'EXECUTE')
            and to_regprocedure('public.claim_next_extract_job(integer)') is not null
            and has_function_privilege(current_user, to_regprocedure('public.claim_next_extract_job(integer)'), 'EXECUTE')
            and to_regprocedure('public.unregister_push_device_for_refresh(text,text)') is not null
            and has_function_privilege(current_user, to_regprocedure('public.unregister_push_device_for_refresh(text,text)'), 'EXECUTE')
          ) as runtime_db_privileges,
          exists (
            select 1 from pg_roles
             where rolname = current_user
               and (rolsuper or rolbypassrls)
          ) as runtime_role_bypasses_rls,
          exists (
            select 1 from information_schema.columns
             where table_schema = 'public'
               and table_name = 'extract_jobs'
               and column_name = 'client_delivery_id'
               and udt_name = 'uuid'
          ) as delivery_column,
          exists (
             select 1
               from pg_index ix
               join pg_class idx on idx.oid = ix.indexrelid
               join pg_class tbl on tbl.oid = ix.indrelid
               join pg_namespace ns on ns.oid = tbl.relnamespace
              where ns.nspname = 'public'
                and tbl.relname = 'extract_jobs'
                and idx.relname = 'extract_jobs_user_delivery_unique'
                and ix.indisunique
                and ix.indisvalid
                and ix.indisready
                and ix.indnkeyatts = 2
                and pg_get_expr(ix.indpred, ix.indrelid) = '(client_delivery_id IS NOT NULL)'
                and array(
                    select attr.attname
                      from unnest(ix.indkey::smallint[]) with ordinality as key(attnum, ord)
                      join pg_attribute attr
                        on attr.attrelid = ix.indrelid and attr.attnum = key.attnum
                     where key.ord <= ix.indnkeyatts
                     order by key.ord
                ) = array['user_id', 'client_delivery_id']::name[]
          ) as delivery_index,
          (
            select count(*) = 3
              from information_schema.columns
             where table_schema = 'public'
               and table_name = 'auth_refresh_tokens'
               and column_name = any(array[
                   'rotation_request_id',
                   'rotation_retry_until',
                   'rotated_token_hash'
               ])
          ) as refresh_replay_columns,
          (
            select count(*) = 5
              from information_schema.columns
             where table_schema = 'public'
               and table_name = 'user_library_state'
               and column_name = any(array['user_id', 'revision', 'snapshot', 'history', 'updated_at'])
          ) as library_state_columns,
          (
            exists (
              select 1 from information_schema.columns
               where table_schema = 'public'
                 and table_name = 'user_library_state'
                 and column_name = 'history'
                 and udt_name = 'jsonb'
                 and is_nullable = 'NO'
            )
            and exists (
              select 1 from pg_constraint
               where conrelid = to_regclass('public.user_library_state')
                 and conname = 'user_library_state_history_is_array'
                 and convalidated
            )
          ) as library_state_history,
          (
            select coalesce(relrowsecurity and relforcerowsecurity, false)
              from pg_class
             where oid = to_regclass('public.user_library_state')
          ) as library_state_rls,
          exists (
            select 1 from pg_policy
             where polrelid = to_regclass('public.user_library_state')
               and polname = 'user_library_state_service'
          ) as library_state_service_policy,
          (
            select count(*) = 5
              from information_schema.columns
             where table_schema = 'public'
               and table_name = 'push_devices'
               and column_name = any(array[
                   'token_hash', 'user_id', 'device_token', 'environment', 'language_code'
               ])
          ) as push_device_columns,
          (
            select count(*) = 13
              from information_schema.columns
             where table_schema = 'public'
               and table_name = 'push_deliveries'
               and column_name = any(array[
                   'id', 'job_id', 'user_id', 'token_hash', 'device_token', 'environment',
                   'status', 'attempts', 'next_attempt_at', 'lease_until', 'sent_at',
                   'last_error_code', 'created_at'
               ])
          ) as push_delivery_columns,
          exists (
            select 1
              from pg_trigger trg
              join pg_class table_ref on table_ref.oid = trg.tgrelid
              join pg_namespace schema_ref on schema_ref.oid = table_ref.relnamespace
             where schema_ref.nspname = 'public'
               and table_ref.relname = 'extract_jobs'
               and trg.tgname = 'extract_job_enqueue_recipe_completion_pushes'
               and trg.tgenabled <> 'D'
               and not trg.tgisinternal
          ) as push_completion_trigger,
          to_regprocedure('public.unregister_push_device_for_refresh(text,text)') is not null
            as push_logout_function,
          exists (
            select 1 from public.app_runtime_heartbeats
             where service_name = 'recipe-worker'
               and last_seen_at > now() - interval '{heartbeat_ttl} seconds'
          ) as worker_heartbeat_recent
        """
    with db_context(actor="service"):
        row = await run_in_threadpool(fetch_one, query)
    if not row or not row.get("delivery_column") or not row.get("delivery_index"):
        raise ValueError("Required import idempotency schema is missing")
    if not row.get("refresh_replay_columns"):
        raise ValueError("Required refresh replay schema is missing")
    if not all(row.get(key) for key in (
        "library_state_columns", "library_state_rls", "library_state_service_policy"
    )):
        raise ValueError("Required user library state schema is missing")
    if not row.get("library_state_history"):
        raise ValueError("Required user library state history migration is missing")
    if not all(row.get(key) for key in (
        "push_device_columns", "push_delivery_columns", "push_completion_trigger", "push_logout_function"
    )):
        raise ValueError("Required push notification schema is missing")
    if row.get("runtime_role_bypasses_rls"):
        raise ValueError("Database role bypasses row-level security")
    if not row.get("runtime_db_privileges"):
        raise ValueError("Runtime database role has incomplete privileges")
    root_pem = settings.apple_root_ca_pem.replace("\\n", "\n").strip()
    if not root_pem:
        raise ValueError("Apple transaction verification certificate is missing")
    try:
        from cryptography import x509

        x509.load_pem_x509_certificate(root_pem.encode("utf-8"))
    except Exception:
        raise ValueError("Apple transaction verification certificate is invalid") from None
    if row.get("runtime_role_bypasses_rls"):
        raise ValueError("Database role bypasses row-level security")
    if not settings.worker_enabled and row.get("worker_heartbeat_recent"):
        raise ValueError("Recipe worker is running while durable worker mode is disabled")
    if settings.apns_enabled and not settings.worker_enabled:
        raise ValueError("APNs requires durable worker mode")
    if settings.apns_enabled:
        from app.apns import APNsClient

        if not APNsClient().enabled:
            raise ValueError("APNs is enabled but its credentials are invalid")
    if settings.worker_enabled and not row.get("worker_heartbeat_recent"):
        raise ValueError("Recipe worker heartbeat is stale")
