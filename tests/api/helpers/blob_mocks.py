"""In-process fake `LocalFsBlobStore.put` patches for Phase D testing.

These context managers swap `data.blob_store.LocalFsBlobStore.put` for a
test double for the duration of an `async with` block. They're used by the
in-process Phase D unit cases that need:

  - **Counting** how many times `put` was called (dedup verification, idempotent
    crash-recovery verification).
  - **Slowing** `put` down with a fixed sleep so we can observe peak concurrency
    via a shared counter (`asyncio.Semaphore(4)` cap proof).
  - **Failing** `put` always or selectively so the retry/dead_letter path runs.

Why patch `put` rather than the whole BlobStore:
  - `_phase_d_upload_one` writes to the `blobs` + `blob_refs` PG tables right
    after `put`. Patching just `put` keeps those PG writes real, so we can
    still assert «no blob row was created when put failed».
  - `presign_get_url` is pure (HMAC over inputs) — leaving it unpatched lets
    the ref envelope built post-success have a real signed URL the test can
    compare bytes against.

Important — these helpers patch the **CLASS** method on `LocalFsBlobStore`,
which means they affect every instance of `LocalFsBlobStore` in this
process. Tests must call `_phase_d_process_row` / `run_phase_d` directly
(in-process, NOT via the live backend on 9011); the backend worker pool
runs in a separate process and isn't affected.

All three context managers restore the original `put` on exit — even on
exception.
"""
from __future__ import annotations

import asyncio
import contextlib
import sys
import threading
from collections.abc import AsyncIterator
from pathlib import Path

# Ensure src-backend on sys.path before importing data.blob_store. Tests already
# do this in their module top-level, but doing it here too means this helper
# can be imported safely from non-test contexts (debugging shells, etc.).
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_SRC_BACKEND = str(_REPO_ROOT / 'src-backend')
if _SRC_BACKEND not in sys.path:
    sys.path.insert(0, _SRC_BACKEND)

from data.blob_store import LocalFsBlobStore  # noqa: E402


@contextlib.asynccontextmanager
async def patch_put_with_counter() -> AsyncIterator[dict]:
    """Replace `LocalFsBlobStore.put` with a wrapper that records each call.

    Yields a state dict with:
      - `call_count` (int) — total invocations
      - `sha256s` (list[str]) — sha256s in call order
      - `unique_sha256s` (set[str]) — distinct sha256 values (dedup proof)

    The original `put` still runs (sha256-keyed dedup behavior preserved), so
    callers can assert «put called twice but underlying file written once» via
    `len(state['sha256s']) == 2` + filesystem check.
    """
    original = LocalFsBlobStore.put
    state: dict = {
        'call_count': 0,
        'sha256s': [],
        'unique_sha256s': set(),
    }
    lock = threading.Lock()

    async def wrapped(self, sha256: str, data: bytes, content_type: str) -> str:
        with lock:
            state['call_count'] += 1
            state['sha256s'].append(sha256)
            state['unique_sha256s'].add(sha256)
        return await original(self, sha256, data, content_type)

    LocalFsBlobStore.put = wrapped  # type: ignore[method-assign]
    try:
        yield state
    finally:
        LocalFsBlobStore.put = original  # type: ignore[method-assign]


@contextlib.asynccontextmanager
async def patch_put_with_delay(
    seconds: float,
) -> AsyncIterator[dict]:
    """Replace `put` with a sleep-then-put wrapper that tracks peak concurrency.

    Yields a state dict with:
      - `current` (int) — # of in-flight puts right now
      - `peak` (int) — high-water mark observed across the run
      - `call_count` (int) — total invocations

    Concurrency cap proof: with `_PHASE_D_CONCURRENCY = 4` and N >= 4 inputs,
    `peak == 4`. The semaphore guards the **single put + per-user blob row
    insert** block, not the whole row-processing pipeline, so even with 16+
    inputs we should never see peak > 4.
    """
    original = LocalFsBlobStore.put
    state: dict = {'current': 0, 'peak': 0, 'call_count': 0}
    lock = asyncio.Lock()

    async def wrapped(self, sha256: str, data: bytes, content_type: str) -> str:
        async with lock:
            state['current'] += 1
            state['call_count'] += 1
            if state['current'] > state['peak']:
                state['peak'] = state['current']
        try:
            await asyncio.sleep(seconds)
            return await original(self, sha256, data, content_type)
        finally:
            async with lock:
                state['current'] -= 1

    LocalFsBlobStore.put = wrapped  # type: ignore[method-assign]
    try:
        yield state
    finally:
        LocalFsBlobStore.put = original  # type: ignore[method-assign]


@contextlib.asynccontextmanager
async def patch_put_always_fails(
    error_message: str = 'simulated put failure',
) -> AsyncIterator[dict]:
    """Replace `put` with a function that always raises RuntimeError.

    Used for dead_letter path coverage: every retry attempt fails →
    _phase_d_process_row records 4-attempt failure entry.

    Yields state dict with:
      - `call_count` (int) — total invocations (each attempt counts).
    """
    original = LocalFsBlobStore.put
    state: dict = {'call_count': 0}
    lock = threading.Lock()

    async def wrapped(self, sha256: str, data: bytes, content_type: str) -> str:
        with lock:
            state['call_count'] += 1
        raise RuntimeError(error_message)

    LocalFsBlobStore.put = wrapped  # type: ignore[method-assign]
    try:
        yield state
    finally:
        LocalFsBlobStore.put = original  # type: ignore[method-assign]


@contextlib.asynccontextmanager
async def patch_put_fails_for_sha(
    bad_shas: set[str], error_message: str = 'simulated targeted failure',
) -> AsyncIterator[dict]:
    """Replace `put` so puts targeting a sha in `bad_shas` always raise; other
    sha's go through normally.

    Used by the «dead_letter doesn't block other attachments in same row» case
    (case 11): one row has 3 attachments, only the middle one (a known sha) is
    forced to fail.

    Yields state dict with:
      - `call_count` (int) — total invocations across all sha
      - `failed_call_count` (int) — invocations that hit a bad sha
    """
    original = LocalFsBlobStore.put
    state: dict = {'call_count': 0, 'failed_call_count': 0}
    lock = threading.Lock()

    async def wrapped(self, sha256: str, data: bytes, content_type: str) -> str:
        with lock:
            state['call_count'] += 1
            if sha256 in bad_shas:
                state['failed_call_count'] += 1
                raise RuntimeError(error_message)
        return await original(self, sha256, data, content_type)

    LocalFsBlobStore.put = wrapped  # type: ignore[method-assign]
    try:
        yield state
    finally:
        LocalFsBlobStore.put = original  # type: ignore[method-assign]
