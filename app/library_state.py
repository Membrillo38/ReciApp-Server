from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import UUID

from app.db import execute_returning, fetch_one


_EMPTY_SNAPSHOT: dict[str, Any] = {
    "schema_version": 1,
    "folders": None,
    "shopping_list": [],
    "preferences": None,
    "cooking_progress": {},
}


def get_user_library_state(user_id: UUID) -> dict[str, Any]:
    row = fetch_one(
        """
        select revision, snapshot, history, updated_at
          from public.user_library_state
         where user_id = %s
         limit 1
        """,
        (user_id,),
    )
    if not row:
        return {"revision": 0, "snapshot": deepcopy(_EMPTY_SNAPSHOT), "history": [], "updated_at": None}
    return row


def save_user_library_state(
    user_id: UUID,
    *,
    expected_revision: int,
    snapshot: dict[str, Any],
) -> dict[str, Any] | None:
    if expected_revision == 0:
        return execute_returning(
            """
            with active_profile as (
                select id
                  from public.profiles
                 where id = %s and deleted_at is null
                 for update
            )
            insert into public.user_library_state (user_id, revision, snapshot, updated_at)
            select active_profile.id, 1, %s, now()
              from active_profile
            on conflict (user_id) do nothing
            returning revision, snapshot, history, updated_at
            """,
            (user_id, snapshot),
        )
    return execute_returning(
        """
        with active_profile as (
            select id
              from public.profiles
             where id = %s and deleted_at is null
             for update
        )
        update public.user_library_state as state
           set snapshot = %s,
               revision = state.revision + 1,
               history = (
                   select coalesce(jsonb_agg(version order by ordinal), '[]'::jsonb)
                     from jsonb_array_elements(
                         coalesce(state.history, '[]'::jsonb) || jsonb_build_array(
                             jsonb_build_object(
                                 'revision', state.revision,
                                 'snapshot', state.snapshot,
                                 'updated_at', state.updated_at
                             )
                         )
                     ) with ordinality as versions(version, ordinal)
                    where ordinal > greatest(jsonb_array_length(coalesce(state.history, '[]'::jsonb)) - 2, 0)
               ),
               updated_at = now()
          from active_profile
         where state.user_id = active_profile.id and state.revision = %s
         returning revision, snapshot, history, updated_at
        """,
        (user_id, snapshot, expected_revision),
    )
