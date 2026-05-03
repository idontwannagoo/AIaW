"""Stage 4.5 / Step 8 — `GET /api/v1/bootstrap`.

One-shot first-screen hydration: returns every small server-routed table
in full plus a per-dialog "recent N messages" slice so the client can
paint the workspaces list / dialogs / latest conversation tail without
N round-trips.

Response shape (mirrors the Step 8 plan section verbatim — adding /
removing top-level keys MUST be reflected in the client decoder):

    {
      "schema_version": int,   # bumped when the wire shape grows new keys
      "workspaces":          [<WorkspaceRow envelope>, ...],
      "dialogs":             [<DialogRow envelope>, ...],
      "providers":           [<ProviderRow envelope>, ...],
      "assistants":          [<AssistantRow envelope>, ...],
      "installed_plugins":   [<InstalledPluginRow envelope>, ...],
      "reactives":           [<ReactiveRow envelope>, ...],
      "avatar_images":       [<AvatarImageRow envelope>, ...],
      "messages_recent":     [<MessageRow envelope>, ...],
    }

Each element of every list is the **same envelope** that the
corresponding `GET /api/v1/<table>` and `realtime` event uses (`{id|key,
version, updated_at, deleted, data}`). This is intentional: the client
decoder is one switch that delegates to the existing `<table>.server.ts`
cache write paths (which already know how to decode wire->IDB shape, e.g.
`materializeAttachment` for messages with `contentsBlob` ref).

Hard exclusions (强约束 / 2026-05-03 plan 修订):

- `items` is NOT included. The items table is dialog-scoped and can carry
  binary attachments; the client should pull it lazily via the scoped
  `?dialogId=` path when entering a dialog.
- `artifacts` is NOT included. Same scoped-pull rationale (workspace-
  scoped lazily by the artifacts.server.ts repo).

Including either would defeat the 1MB response budget on heavy users.
The contract is asserted by `tests/api/test_bootstrap.py::
test_returns_all_small_tables_in_one_response` (which must check that
the response field set does NOT contain those keys, so a future
"convenience" addition fails CI loudly).

`messages_recent` algorithm:

1. Rank dialogs by `MAX(messages.updated_at)` DESC (most recently active
   first; dialogs with no messages have rank = NULL and are skipped).
2. For each ranked dialog, fetch the most recent 50 messages
   (`ORDER BY updated_at DESC LIMIT 50`).
3. Append envelopes to `messages_recent` while accumulating the running
   serialized byte count. As soon as the projected total response size
   would exceed `_RESPONSE_BYTE_BUDGET`, stop adding more dialogs (the
   in-progress dialog is also dropped — we never partially include a
   dialog's tail because that would be a misleading "you have 7 of 50
   messages" view).

Cache header: `Cache-Control: private, max-age=10` so a navigation that
re-mounts the layout doesn't re-hit the endpoint within 10s. `private`
because the response is account-scoped via auth.

Active import job compatibility (plan Step 8 last bullet): bootstrap
queries `WHERE deleted_at IS NULL`, so it returns whatever Phase B has
finished writing. The progress card is fed by the `import_jobs`
realtime channel, not by bootstrap, so there is no coupling.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Response
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import current_user
from ..db import get_session
from ..models.assistant import Assistant
from ..models.avatar_image import AvatarImage
from ..models.dialog import Dialog
from ..models.installed_plugin import InstalledPlugin
from ..models.message import Message
from ..models.provider import Provider
from ..models.reactive import Reactive
from ..models.user import User
from ..models.workspace import Workspace

router = APIRouter(prefix='/api/v1', tags=['bootstrap'])


# Bump when the bootstrap wire shape grows a new top-level key (or removes
# / renames one). Clients that see an unrecognised version log a warning
# and still attempt to apply the keys they recognise — this is a soft
# coordination hint, not a hard gate.
_SCHEMA_VERSION = 1

# 1MB hard ceiling on the JSON response body. The plan calls out 200KB
# typical and 1MB as the hard cap; we use 1_000_000 (decimal) to leave
# headroom for response framing / gzip metadata vs the wire MTU. The
# truncation algorithm checks projected size *before* appending each
# dialog's tail so we never overshoot mid-dialog.
_RESPONSE_BYTE_BUDGET = 1_000_000

# Cap per-dialog tail at 50 newest messages. A dialog with > 50 messages
# returns its 50 newest; older messages come down via scoped-pull when
# the user actually scrolls back into the dialog.
_MESSAGES_PER_DIALOG = 50


def _user_id(user: User = Depends(current_user)) -> str:
    return user.id


def _envelope_id(row: Any, deleted: bool) -> dict[str, Any]:
    """Build the {id, version, updated_at, deleted, data} envelope used by
    every id-PK table (workspaces / dialogs / providers / assistants /
    avatar_images / messages). Mirrors `<table>.py::_to_row`.
    """
    return {
        'id': row.id,
        'version': row.version,
        'updated_at': row.updated_at.isoformat(),
        'deleted': deleted,
        'data': None if deleted else row.data,
    }


def _envelope_kv(row: Any, deleted: bool) -> dict[str, Any]:
    """KV table envelope variant: `key` takes the natural identity slot.
    Mirrors `installed_plugins.py::_to_row` / `reactives.py::_to_row`.
    """
    return {
        'key': row.key,
        'version': row.version,
        'updated_at': row.updated_at.isoformat(),
        'deleted': deleted,
        'data': None if deleted else row.data,
    }


async def _list_id_table(
    session: AsyncSession,
    model: Any,
    user_id: str,
) -> list[dict[str, Any]]:
    """`SELECT * FROM <table> WHERE user_id = :u AND deleted_at IS NULL
    ORDER BY version` — same predicate as the per-table list endpoints
    use for the un-paged full pull.
    """
    stmt = (
        select(model)
        .where(model.user_id == user_id, model.deleted_at.is_(None))
        .order_by(model.version)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [_envelope_id(r, deleted=False) for r in rows]


async def _list_kv_table(
    session: AsyncSession,
    model: Any,
    user_id: str,
) -> list[dict[str, Any]]:
    stmt = (
        select(model)
        .where(model.user_id == user_id, model.deleted_at.is_(None))
        .order_by(model.version)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [_envelope_kv(r, deleted=False) for r in rows]


async def _gather_messages_recent(
    session: AsyncSession,
    user_id: str,
    base_size: int,
) -> list[dict[str, Any]]:
    """Build the per-dialog tail respecting the 1MB budget.

    `base_size` is the JSON byte size of the response *without*
    messages_recent (i.e. the sum of all small tables already serialized
    plus the schema_version key + bracket / brace overhead). We start with
    that and stop appending dialog-tails when adding the next dialog
    would overshoot `_RESPONSE_BYTE_BUDGET`.
    """
    # Rank dialogs by their most-recent message activity. We aggregate
    # over `messages` (skipping tombstones) and group by dialog_id; rows
    # come back in `last_at DESC` order so we hand the most recently
    # active dialog to the truncation loop first. Dialogs with zero non-
    # deleted messages don't appear at all (the GROUP BY result is
    # empty for them).
    last_at = func.max(Message.updated_at).label('last_at')
    rank_stmt = (
        select(Message.dialog_id, last_at)
        .where(
            Message.user_id == user_id,
            Message.deleted_at.is_(None),
        )
        .group_by(Message.dialog_id)
        .order_by(desc(last_at))
    )
    ranked = (await session.execute(rank_stmt)).all()
    if not ranked:
        return []

    # Resolve the dialog ids actually owned by this user + not deleted.
    # Defense-in-depth (the messages query already had user_id), but
    # cheaper than a join in the rank query.
    dialog_ids = [r.dialog_id for r in ranked]
    owned_stmt = select(Dialog.id).where(
        Dialog.id.in_(dialog_ids),
        Dialog.user_id == user_id,
        Dialog.deleted_at.is_(None),
    )
    owned = {row for row, in (await session.execute(owned_stmt)).all()}

    out: list[dict[str, Any]] = []
    running = base_size
    for r in ranked:
        if r.dialog_id not in owned:
            continue
        # Pull this dialog's 50 newest messages.
        tail_stmt = (
            select(Message)
            .where(
                Message.user_id == user_id,
                Message.dialog_id == r.dialog_id,
                Message.deleted_at.is_(None),
            )
            .order_by(desc(Message.updated_at))
            .limit(_MESSAGES_PER_DIALOG)
        )
        tail = (await session.execute(tail_stmt)).scalars().all()
        if not tail:
            continue
        envelopes = [_envelope_id(m, deleted=False) for m in tail]
        # Serialize once to measure size (and the response builder will
        # serialize again — small inefficiency, but the alternative is a
        # second JSON pass over the whole response which is worse on
        # heavy users). Add per-element ", " framing budget conservatively.
        chunk_size = sum(
            len(json.dumps(e, separators=(',', ':'))) + 2 for e in envelopes
        )
        if running + chunk_size > _RESPONSE_BYTE_BUDGET:
            # Truncate at the *dialog* boundary, not partway through a
            # tail. See module docstring for rationale.
            break
        out.extend(envelopes)
        running += chunk_size
    return out


@router.get('/bootstrap')
async def bootstrap(
    response: Response,
    user_id: str = Depends(_user_id),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    # Order matters only for cache header / serialization predictability,
    # not correctness — every list is independent at the SQL layer.
    workspaces = await _list_id_table(session, Workspace, user_id)
    dialogs = await _list_id_table(session, Dialog, user_id)
    providers = await _list_id_table(session, Provider, user_id)
    assistants = await _list_id_table(session, Assistant, user_id)
    avatar_images = await _list_id_table(session, AvatarImage, user_id)
    installed_plugins = await _list_kv_table(session, InstalledPlugin, user_id)
    reactives = await _list_kv_table(session, Reactive, user_id)

    # Measure base size before adding messages_recent so the truncation
    # algorithm has an honest starting point.
    base_payload: dict[str, Any] = {
        'schema_version': _SCHEMA_VERSION,
        'workspaces': workspaces,
        'dialogs': dialogs,
        'providers': providers,
        'assistants': assistants,
        'installed_plugins': installed_plugins,
        'reactives': reactives,
        'avatar_images': avatar_images,
        'messages_recent': [],
    }
    base_size = len(json.dumps(base_payload, separators=(',', ':')))
    base_payload['messages_recent'] = await _gather_messages_recent(
        session, user_id, base_size
    )

    # 10s private cache: a layout remount within 10s reuses the response
    # without bothering the backend; cross-account isolation is preserved
    # by `private` (no shared cache may store this).
    response.headers['Cache-Control'] = 'private, max-age=10'
    # Bug 2 same-tab cross-account leak fix: `private` only blocks shared
    # caches; the browser cache is keyed on URL (not the Authorization
    # header) by default, so a logout-then-login-as-different-user inside
    # the 10s window would otherwise serve account A's response to
    # account B. `Vary: Authorization` forces the browser to treat
    # responses with different bearers as distinct cache entries, killing
    # the cross-account leak without disabling the cache for the common
    # single-user remount case.
    response.headers['Vary'] = 'Authorization'
    return base_payload
