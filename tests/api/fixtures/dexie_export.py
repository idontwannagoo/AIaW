"""dexie-export-import wire format fixture builders for ImportJob tests.

dexie-export-import (https://github.com/dexie/Dexie.js/tree/master/addons/dexie-export-import)
serializes a Dexie database as:

    {
      "formatName":    "dexie",
      "formatVersion": 1,
      "data": {
        "databaseName":    "<db-name>",
        "databaseVersion": <int>,
        "tables": [
          {"name": "<table>", "schema": "<dexie-schema-string>", "rowCount": <int>},
          ...
        ],
        "data": [
          {"tableName": "<table>", "inbound": <bool>, "rows": [<row>, ...]},
          ...
        ]
      }
    }

(`inbound: true` means the row dicts include their own primary key value;
`inbound: false` means the array entries are `{key, value}` pairs — AIaW's
schema doesn't use the latter for any table we care about, so fixtures here
always emit `inbound: true`.)

Step 1 of Stage 4.5 only needs two fixture sizes:

    - small (~10 providers + 5 dialogs, no attachments) for fast assertion
      tests on Phase A row counts and table-split correctness
    - huge  (~200MB JSON, 10000 messages + 100 attachments × ~2MB) for the
      `slow`-marked memory-bound test that asserts ijson stays under the
      200MB RSS budget

medium / large are placeholders for Steps 3 / 4 / 5 — they share the same
shape so adding sizes later is just a row-count knob.
"""
from __future__ import annotations

import base64
import io
import json
import os
import secrets
from collections.abc import Iterable, Iterator
from typing import Any


# ---- header / table schema strings -----------------------------------------


# These schema strings mirror what dexie-export-import writes when serializing
# the `db` from src/utils/db.ts. They're parsed only by Dexie's import path —
# the backend ImportJob worker treats them as opaque (Phase A walks only
# `data.data[*].rows`). Keeping them realistic keeps fixtures debuggable in a
# real Dexie if anyone ever side-loads one.
_TABLE_SCHEMAS: dict[str, str] = {
    'providers': '++id,name',
    'workspaces': '++id,parentId',
    'dialogs': 'id,workspaceId,assistantId',
    'messages': 'id,dialogId,type',
    'assistants': 'id,workspaceId',
    'avatarImages': 'id',
    'installedPluginsV2': 'id',
    'items': 'id,type',
    'artifacts': 'id,workspaceId',
    'reactives': 'key',
}


def _envelope(
    table_chunks: list[dict[str, Any]],
    *,
    db_name: str = 'aiaw',
    db_version: int = 6,
) -> dict[str, Any]:
    """Wrap per-table {tableName, rows} chunks in the dexie-export-import
    outer envelope. Centralized so tests don't reach into wire format."""
    tables_meta = [
        {
            'name': chunk['tableName'],
            'schema': _TABLE_SCHEMAS.get(chunk['tableName'], ''),
            'rowCount': len(chunk['rows']),
        }
        for chunk in table_chunks
    ]
    return {
        'formatName': 'dexie',
        'formatVersion': 1,
        'data': {
            'databaseName': db_name,
            'databaseVersion': db_version,
            'tables': tables_meta,
            'data': [
                {
                    'tableName': chunk['tableName'],
                    'inbound': True,
                    'rows': chunk['rows'],
                }
                for chunk in table_chunks
            ],
        },
    }


# ---- row factories ---------------------------------------------------------


def _gen_id(prefix: str) -> str:
    return f'{prefix}-{secrets.token_hex(8)}'


def _provider_row(i: int) -> dict[str, Any]:
    return {
        'id': _gen_id('prov'),
        'name': f'Test Provider {i}',
        'type': 'openai',
        'config': {'apiKey': 'sk-test-' + secrets.token_hex(8)},
    }


def _dialog_row(i: int, workspace_id: str) -> dict[str, Any]:
    return {
        'id': _gen_id('dlg'),
        'workspaceId': workspace_id,
        'name': f'Dialog {i}',
        'assistantId': None,
        'msgTree': {},
        'msgRoute': [],
        'msgBranchState': {},
        'inputVars': {},
    }


def _workspace_row(i: int) -> dict[str, Any]:
    return {
        'id': _gen_id('ws'),
        'name': f'Workspace {i}',
        'parentId': '$root',
        'avatar': {'type': 'icon', 'icon': 'sym_o_folder'},
        'vars': {},
        'indexContent': '',
    }


def _message_row(i: int, dialog_id: str, attachment_b64: str | None = None) -> dict[str, Any]:
    contents: list[dict[str, Any]] = [
        {'type': 'user-message', 'text': f'message {i}'}
    ]
    row: dict[str, Any] = {
        'id': _gen_id('msg'),
        'dialogId': dialog_id,
        'type': 'user',
        'contents': contents,
        'status': 'default',
    }
    if attachment_b64 is not None:
        row['attachment'] = {
            'type': 'inline',
            'contentType': 'application/octet-stream',
            'data': attachment_b64,
        }
    return row


# ---- public API ------------------------------------------------------------


def make_small_export() -> dict[str, Any]:
    """10 providers + 5 dialogs (one workspace) + 0 messages + 0 attachments.

    Returns a Python dict — caller json.dumps + writes wherever it likes
    (BlobStore for end-to-end, plain bytes for unit tests of the parser).
    Total payload < 10KB.
    """
    ws = _workspace_row(0)
    providers = [_provider_row(i) for i in range(10)]
    dialogs = [_dialog_row(i, ws['id']) for i in range(5)]
    return _envelope(
        [
            {'tableName': 'providers', 'rows': providers},
            {'tableName': 'workspaces', 'rows': [ws]},
            {'tableName': 'dialogs', 'rows': dialogs},
        ]
    )


def make_medium_export(
    *,
    messages: int = 5,
    attachment_size_mb: int = 1,
) -> bytes:
    """1 workspace + 1 dialog + N messages each with `attachment_size_mb`-MB
    attachment, returned as raw dexie-export-import bytes ready for BlobStore
    upload.

    Sized for Stage 4.5 / Step 7 `test_phase_d_complete_makes_attachment_renderable`:
    we want enough rows to produce real Phase B/C/D progress events and at
    least one `≥ 64KB` attachment so Phase D actually exercises the BlobStore
    upload path. Default 5 × 1MB = ~5-7MB total payload (1.33x base64 overhead),
    which fits comfortably in two 5MB multipart parts.

    Returned as bytes (not Iterator[bytes]) because medium fixtures are small
    enough to materialize once and keep around for the test duration. Callers
    that want streaming should use `iter_huge_export_bytes` instead.
    """
    ws = _workspace_row(0)
    dlg = _dialog_row(0, ws['id'])
    bytes_per_attachment = attachment_size_mb * 1024 * 1024

    msg_rows: list[dict[str, Any]] = []
    for i in range(messages):
        # Distinct random bytes per attachment so dedupe doesn't collapse to
        # one — Phase D test wants to verify each attachment is independently
        # uploaded + assignable, and dedupe logic is its own test.
        raw = os.urandom(bytes_per_attachment)
        b64 = base64.b64encode(raw).decode('ascii')
        msg_rows.append(_message_row(i, dlg['id'], attachment_b64=b64))

    export = _envelope(
        [
            {'tableName': 'workspaces', 'rows': [ws]},
            {'tableName': 'dialogs', 'rows': [dlg]},
            {'tableName': 'messages', 'rows': msg_rows},
        ]
    )
    # Single allocation is fine here — even at 5 × 1MB the JSON string is
    # ~7MB which is small relative to pytest worker RSS budget.
    return json.dumps(export, separators=(',', ':')).encode('utf-8')


# Huge fixture knobs — exposed so tests can override for faster runs without
# editing this module. Defaults aim for ~200MB.
HUGE_MESSAGE_COUNT = int(os.environ.get('IMPORT_HUGE_MESSAGES', '10000'))
HUGE_ATTACHMENT_COUNT = int(os.environ.get('IMPORT_HUGE_ATTACHMENTS', '100'))
HUGE_ATTACHMENT_BYTES = int(
    os.environ.get('IMPORT_HUGE_ATTACHMENT_BYTES', str(2 * 1024 * 1024))
)


def iter_huge_export_bytes(
    *,
    message_count: int = HUGE_MESSAGE_COUNT,
    attachment_count: int = HUGE_ATTACHMENT_COUNT,
    attachment_bytes: int = HUGE_ATTACHMENT_BYTES,
) -> Iterator[bytes]:
    """Stream-generate the huge dexie-export-import payload as bytes.

    Yields chunks so callers (BlobStore put / file write) never materialize
    the whole 200MB in RAM. Pure generator — no file IO inside.

    Layout:
      - 1 workspace
      - 1 dialog
      - `attachment_count` messages with inline base64 attachments of
        `attachment_bytes` raw bytes each (≈ 1.33x size after base64)
      - `message_count - attachment_count` plain text messages

    The output JSON is hand-rolled because `json.dumps(big_list)` would
    materialize the entire list. We emit:

        {"formatName":"dexie","formatVersion":1,"data":{
            "databaseName":"aiaw","databaseVersion":6,
            "tables":[...],
            "data":[
              {"tableName":"workspaces","inbound":true,"rows":[<ws>]},
              {"tableName":"dialogs",   "inbound":true,"rows":[<dlg>]},
              {"tableName":"messages",  "inbound":true,"rows":[<msg>,<msg>,...]}
            ]
          }
        }
    """
    ws = _workspace_row(0)
    dlg = _dialog_row(0, ws['id'])

    # tables metadata uses real rowCount so the schema is internally
    # consistent — Phase A doesn't trust this, but a real Dexie import would.
    tables_meta = [
        {'name': 'workspaces', 'schema': _TABLE_SCHEMAS['workspaces'], 'rowCount': 1},
        {'name': 'dialogs', 'schema': _TABLE_SCHEMAS['dialogs'], 'rowCount': 1},
        {'name': 'messages', 'schema': _TABLE_SCHEMAS['messages'], 'rowCount': message_count},
    ]

    # Header.
    header = (
        '{"formatName":"dexie","formatVersion":1,"data":{'
        f'"databaseName":"aiaw","databaseVersion":6,'
        f'"tables":{json.dumps(tables_meta, separators=(",", ":"))},'
        '"data":['
    )
    yield header.encode('utf-8')

    # workspaces chunk.
    yield (
        '{"tableName":"workspaces","inbound":true,"rows":['
        + json.dumps(ws, separators=(',', ':'))
        + ']},'
    ).encode('utf-8')

    # dialogs chunk.
    yield (
        '{"tableName":"dialogs","inbound":true,"rows":['
        + json.dumps(dlg, separators=(',', ':'))
        + ']},'
    ).encode('utf-8')

    # messages chunk — open the rows array then stream rows one by one.
    yield b'{"tableName":"messages","inbound":true,"rows":['

    for i in range(message_count):
        attach_b64: str | None = None
        if i < attachment_count:
            # Generate distinct bytes per attachment so dedupe doesn't
            # collapse them to one. urandom is fine here — the worker just
            # measures size, not content.
            raw = os.urandom(attachment_bytes)
            attach_b64 = base64.b64encode(raw).decode('ascii')
        row = _message_row(i, dlg['id'], attachment_b64=attach_b64)
        sep = b',' if i > 0 else b''
        yield sep + json.dumps(row, separators=(',', ':')).encode('utf-8')

    yield b']}'  # close messages chunk + rows array
    yield b']}}'  # close data array + outer data object + outer envelope


def write_huge_export_to_path(
    path: str,
    *,
    message_count: int = HUGE_MESSAGE_COUNT,
    attachment_count: int = HUGE_ATTACHMENT_COUNT,
    attachment_bytes: int = HUGE_ATTACHMENT_BYTES,
) -> int:
    """Write the huge fixture to disk; return total bytes written.

    Use this from a session-scoped pytest fixture so the 200MB blob is
    generated once per test run, not per case. Tests that need to point the
    BlobStore at it can sha256 the file post-write (or compute streaming).
    """
    total = 0
    with open(path, 'wb') as f:
        for chunk in iter_huge_export_bytes(
            message_count=message_count,
            attachment_count=attachment_count,
            attachment_bytes=attachment_bytes,
        ):
            f.write(chunk)
            total += len(chunk)
    return total


def export_dict_to_bytes(export: dict[str, Any]) -> bytes:
    """Convenience: small-fixture dict → bytes the BlobStore.put path
    accepts. Single allocation, fine for the small fixture (< 10KB)."""
    return json.dumps(export, separators=(',', ':')).encode('utf-8')


def collect_iter_to_bytes(it: Iterable[bytes]) -> bytes:
    """Drain a chunk iterator into a single bytes object. Only safe for the
    small fixture; do NOT call on iter_huge_export_bytes() — that defeats
    the streaming design."""
    buf = io.BytesIO()
    for chunk in it:
        buf.write(chunk)
    return buf.getvalue()
