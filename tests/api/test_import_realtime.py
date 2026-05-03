"""Stage 4.5 / Step 6 — WS 进度推送 + 状态持久化（集成验证）.

Maps plan line 1263-1269 通过判据 + dev 给 test agent 的 8 条扩展判据:

  1. test_phase_change_publishes_ws_event
       — drive a real job through complete → worker phases; subscribe to
         import_jobs WS channel; collect events; assert observed `data.status`
         values form a monotonic phase progression containing at minimum
         {queued, phase_b, done}. parsing / phase_c / phase_d are DB-only
         transitions (no broker.publish) for the fixture sizes here, so we
         don't require them — only forward progress.
  2. test_ws_event_envelope_matches_routed_table_contract
       — single event from above run; superset assertions on envelope shape:
         top-level {id, version, updated_at, deleted=False, data}
         data superset of ImportJob._envelope()['data']
         (Phase B publishes inject `phase_b_table` extra; we use superset).
  3. test_user_b_does_not_receive_user_a_import_progress
       — A drives import; B subscribes import_jobs and waits 4s after own
         replay-done — must see zero import_jobs events. broker.publish is
         per-user; B's WS sub stays empty.
  4. test_active_jobs_query_returns_in_progress_only_cross_user
       — A creates an active job; B GET /api/v1/import/jobs?status=active
         returns []. Step 2 already covered same-user behavior; here we
         lock the cross-user isolation contract.
  5. test_client_put_to_import_jobs_returns_405
       — PUT/PATCH/POST/DELETE on /api/v1/import_jobs/<any-id> all return
         405 + body containing 'read-only' + Allow header present.
  6. test_get_to_import_jobs_path_also_405_or_404
       — GET /api/v1/import_jobs/<id> returns 405 (FastAPI default for an
         api_route registered without GET); the business path
         GET /api/v1/import/jobs/<id> still returns 200.
  7. test_sse_subscribe_import_jobs_works
       — SSE channel mirrors WS: subscribe import_jobs, drive a job to done,
         confirm at least one event arrives with table='import_jobs'.
  8. test_replay_after_reconnect_picks_up_missed_events
       — A subscribes since=0 → drives complete → reads first batch → notes
         max rev → disconnects → drives more progress → reconnects with
         since=<max rev> → replay events all have rev > <max rev>.

Approach:
  - Drive jobs end-to-end via the real REST + multipart upload (same path as
    test_imports_router.py) so the worker actually publishes via broker. The
    in-process direct-driver pattern (used by test_import_phase_*.py) bypasses
    `notify_pending()` + the per-process broker; we want the on-the-wire
    contract here.
  - Uses fixtures.dexie_export.make_small_export() — produces ~3KB upload
    that worker drains in <2s on test rig (Phase A + B + cleanup).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any
from urllib.parse import urlparse

import httpx
import pytest

from .conftest import BACKEND_URL
from .fixtures.dexie_export import export_dict_to_bytes, make_small_export
from .test_realtime_sse import SseReader, _open_sse


# ---- helpers ----------------------------------------------------------------


async def _full_upload(client: httpx.AsyncClient) -> str:
    """Drive a small dexie export end-to-end through the multipart endpoints.
    Returns job_id. After this returns the worker may still be running phases
    (caller should subscribe BEFORE calling, to capture events).
    """
    payload = make_small_export()
    raw = export_dict_to_bytes(payload)

    create = await client.post(
        '/api/v1/import/jobs', json={'file_size': len(raw)},
    )
    assert create.status_code == 201, (
        f'create failed: {create.status_code} {create.text}'
    )
    job = create.json()

    # Single part upload (raw is a few KB; one part is fine for these tests).
    part = await client.post(
        f'/api/v1/import/jobs/{job["job_id"]}/parts/1',
    )
    assert part.status_code == 200, part.text
    part_url = part.json()['upload_url']

    async with httpx.AsyncClient(timeout=10.0) as upload_client:
        put = await upload_client.put(part_url, content=raw)
        assert put.status_code == 200, f'part PUT failed: {put.status_code} {put.text}'
        etag = put.json()['etag']

    complete = await client.post(
        f'/api/v1/import/jobs/{job["job_id"]}/complete',
        json={'parts': [{'part_number': 1, 'etag': etag}]},
    )
    assert complete.status_code == 200, complete.text
    return job['job_id']


async def _drain_import_jobs_events(
    ws,
    *,
    timeout: float = 6.0,
    until_status: str | None = None,
) -> list[dict[str, Any]]:
    """Pull frames off `ws` for up to `timeout` seconds, collecting all
    import_jobs events. Stops early when an event with `data.status == until_status`
    arrives. Skips replay-done / ping / non-import_jobs frames.
    """
    events: list[dict[str, Any]] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except asyncio.TimeoutError:
            break
        msg = json.loads(raw)
        if msg.get('type') == 'event' and msg.get('table') == 'import_jobs':
            events.append(msg)
            row = msg.get('row') or {}
            data = row.get('data') or {}
            if until_status is not None and data.get('status') == until_status:
                break
    return events


# ---- 1. phase change publishes WS event ------------------------------------


async def test_phase_change_publishes_ws_event(
    user_a, client_a: httpx.AsyncClient, ws_connect,
) -> None:
    """End-to-end: subscribe import_jobs → upload + complete → collect ≥3
    events covering forward phase progression including queued + phase_b + done.

    Phase A→B and B→C and C→D status flips are DB-only (no broker.publish)
    — only progress events from inside Phase B/C/D fire on the wire, plus the
    explicit complete-time `queued` snapshot and end-of-phase-D `done` snapshot.
    For the small fixture (1 workspace + 5 dialogs + 0 messages + 0 attachments)
    we expect to see queued + phase_b (per-table progress) + done at minimum.
    """
    async with ws_connect(user_a['access_token']) as ws:
        await ws.send(json.dumps(
            {'type': 'subscribe', 'table': 'import_jobs', 'since': 0}
        ))
        # Drain replay-done (empty table).
        first = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
        assert first == {'type': 'replay-done', 'table': 'import_jobs', 'rev': 0}, (
            f'unexpected first frame: {first!r}'
        )

        job_id = await _full_upload(client_a)

        events = await _drain_import_jobs_events(
            ws, timeout=8.0, until_status='done',
        )

        statuses = [(e['row']['data']['status'], e['rev']) for e in events]
        ids = {e['row']['data']['id'] for e in events}
        assert ids == {job_id}, (
            f'all events should be for this job; got ids={ids} '
            f'expected={{{job_id}}}; full statuses={statuses}'
        )

        observed = {s for s, _ in statuses}
        # Forward progress: queued (from POST /complete) + phase_b (per-table
        # progress publish) + done (final publish). parsing/phase_c/phase_d
        # are not separately published and aren't required here.
        required = {'queued', 'phase_b', 'done'}
        missing = required - observed
        assert not missing, (
            f'missing required statuses {missing}; full sequence={statuses}'
        )

        # Monotonic rev: each event's rev should be > previous (server-routed
        # tables share global_change_seq, so rev only ever increases).
        revs = [r for _, r in statuses]
        assert revs == sorted(revs), (
            f'revs not monotonic non-decreasing: {revs} (events={statuses})'
        )


# ---- 2. envelope contract ---------------------------------------------------


async def test_ws_event_envelope_matches_routed_table_contract(
    user_a, client_a: httpx.AsyncClient, ws_connect,
) -> None:
    """One event from a real run; assert the wire shape matches what other
    server-routed tables (providers / messages / etc.) publish so the frontend
    realtime handler can decode it via the unified code path.
    """
    async with ws_connect(user_a['access_token']) as ws:
        await ws.send(json.dumps(
            {'type': 'subscribe', 'table': 'import_jobs', 'since': 0}
        ))
        await asyncio.wait_for(ws.recv(), timeout=5.0)  # replay-done

        job_id = await _full_upload(client_a)

        events = await _drain_import_jobs_events(
            ws, timeout=8.0, until_status='done',
        )
        assert events, 'no import_jobs events received'

    # Pick the LAST event (status='done' or last seen) — guaranteed to have
    # all counters filled. This avoids the slim chance that the very first
    # `queued` snapshot has total_rows=NULL etc. Both shapes must satisfy the
    # contract so first-event would also work; we just want a stable pick.
    last = events[-1]

    # Top-level envelope
    assert last['type'] == 'event', last
    assert last['table'] == 'import_jobs', last
    assert last['op'] == 'put', last
    assert last['id'] == job_id, last
    assert isinstance(last['rev'], int) and last['rev'] > 0, last

    row = last['row']
    assert isinstance(row, dict), f'row not dict: {row!r}'
    # Same envelope keys as other server-routed tables (providers/messages/...)
    expected_top_level = {'id', 'version', 'updated_at', 'deleted', 'data'}
    missing_top = expected_top_level - set(row.keys())
    assert not missing_top, f'envelope missing keys {missing_top}; row={row}'
    assert row['id'] == job_id
    assert row['deleted'] is False
    assert isinstance(row['version'], int) and row['version'] > 0
    assert isinstance(row['updated_at'], str) and len(row['updated_at']) > 0

    data = row['data']
    assert isinstance(data, dict)
    # Superset assertion: required ImportJob snapshot keys (Phase B publishes
    # inject extra `phase_b_table` so strict-equal would be wrong).
    required_data_keys = {
        'id', 'status', 'multipart_upload_id', 'raw_object_key',
        'total_bytes', 'processed_bytes', 'total_rows', 'processed_rows',
        'total_blobs', 'processed_blobs', 'error_message', 'dead_letter',
        'created_at', 'updated_at',
    }
    missing_data = required_data_keys - set(data.keys())
    assert not missing_data, (
        f'data envelope missing keys {missing_data}; data={data}'
    )
    assert data['id'] == job_id
    assert data['status'] in {
        'queued', 'parsing', 'phase_b', 'phase_c', 'phase_d', 'done',
    }, data
    assert isinstance(data['dead_letter'], list)


# ---- 3. cross-user isolation: B does NOT see A's import progress -----------


async def test_user_b_does_not_receive_user_a_import_progress(
    user_a, user_b,
    client_a: httpx.AsyncClient, ws_connect,
) -> None:
    """A drives an import; B subscribes import_jobs first (drains its own
    empty replay-done), then waits — must NOT receive any import_jobs event.

    broker.publish is per-user via `_subs.get(user_id, ())`; A's events go
    only to A's subscription bucket.
    """
    async with ws_connect(user_b['access_token']) as ws_b:
        await ws_b.send(json.dumps(
            {'type': 'subscribe', 'table': 'import_jobs', 'since': 0}
        ))
        # Drain B's replay-done before A starts to avoid mistaking the
        # bootstrap frame for an event.
        first = json.loads(await asyncio.wait_for(ws_b.recv(), timeout=5.0))
        assert first == {'type': 'replay-done', 'table': 'import_jobs', 'rev': 0}, (
            f'B unexpected first frame: {first!r}'
        )

        # A drives the import. Don't subscribe A here — we don't care about
        # A's events; we care that B sees none of them.
        job_id = await _full_upload(client_a)

        # Wait for A's job to actually drive progress on the broker. We can't
        # observe A's events directly (no A subscription), but we can poll
        # GET /import/jobs/<id> until status reaches done, which guarantees
        # all of A's broker.publish calls have happened.
        async def _wait_a_done() -> None:
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                r = await client_a.get(f'/api/v1/import/jobs/{job_id}')
                if r.status_code == 200 and r.json().get('status') == 'done':
                    return
                await asyncio.sleep(0.2)
            raise AssertionError(
                f'A job {job_id} did not reach done within 8s; '
                f'last status={r.json().get("status") if r.status_code == 200 else r.status_code}'
            )

        await _wait_a_done()

        # Now drain B for up to 2.5s — anything B receives is a leak. Use a
        # generous window after _wait_a_done returns so any in-flight broker
        # publish has time to (incorrectly) fan out to B if isolation breaks.
        leaked: list[dict[str, Any]] = []
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws_b.recv(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            msg = json.loads(raw)
            if msg.get('type') == 'event' and msg.get('table') == 'import_jobs':
                leaked.append(msg)

        assert not leaked, (
            f'B received {len(leaked)} import_jobs events from A; '
            f'leaked sample={leaked[:2]}'
        )


# ---- 4. active jobs cross-user isolation -----------------------------------


async def test_active_jobs_query_returns_in_progress_only_cross_user(
    user_a, user_b,
    client_a: httpx.AsyncClient, client_b: httpx.AsyncClient,
) -> None:
    """A has an active import job; B GET ?status=active returns []. Step 2
    test_get_active_jobs_returns_in_progress_only covered same-user; this
    locks B never sees A's job in the query.
    """
    create = await client_a.post(
        '/api/v1/import/jobs', json={'file_size': 1024},
    )
    assert create.status_code == 201, create.text

    # A sees own active job
    r_a = await client_a.get('/api/v1/import/jobs?status=active')
    assert r_a.status_code == 200
    assert len(r_a.json()) == 1, r_a.json()

    # B sees []
    r_b = await client_b.get('/api/v1/import/jobs?status=active')
    assert r_b.status_code == 200, r_b.text
    body_b = r_b.json()
    assert body_b == [], (
        f'B should not see A active job; got {body_b}'
    )


# ---- 5. client write to import_jobs returns 405 ----------------------------


async def test_client_put_to_import_jobs_returns_405(
    user_a, client_a: httpx.AsyncClient,
) -> None:
    """import_jobs is a read-only realtime channel: PUT/PATCH/POST/DELETE on
    /api/v1/import_jobs/<id> all return 405 + body 'read-only' + Allow header
    present (even if empty). 405 (not 404) makes the contract loud — silent
    404 would mask a real bug in retry logic.
    """
    fake_id = 'a' * 36

    # PUT
    r_put = await client_a.put(
        f'/api/v1/import_jobs/{fake_id}',
        json={'data': {'status': 'done'}},
    )
    assert r_put.status_code == 405, (
        f'PUT expected 405, got {r_put.status_code}: {r_put.text}'
    )
    assert 'read-only' in r_put.text.lower(), r_put.text
    # FastAPI's default 405 handler also sets Allow; the explicit Allow:''
    # the route handler returns wins. Either way the header should exist.
    assert 'allow' in {h.lower() for h in r_put.headers.keys()}, (
        f'Allow header missing on 405; headers={dict(r_put.headers)}'
    )

    # PATCH
    r_patch = await client_a.patch(
        f'/api/v1/import_jobs/{fake_id}',
        json={'status': 'done'},
    )
    assert r_patch.status_code == 405, r_patch.text
    assert 'read-only' in r_patch.text.lower(), r_patch.text

    # POST
    r_post = await client_a.post(
        f'/api/v1/import_jobs/{fake_id}',
        json={'status': 'done'},
    )
    assert r_post.status_code == 405, r_post.text
    assert 'read-only' in r_post.text.lower(), r_post.text

    # DELETE
    r_delete = await client_a.delete(f'/api/v1/import_jobs/{fake_id}')
    assert r_delete.status_code == 405, r_delete.text
    assert 'read-only' in r_delete.text.lower(), r_delete.text


# ---- 6. GET on read-only path also rejected; business path still works -----


async def test_get_to_import_jobs_path_also_405_or_404(
    user_a, client_a: httpx.AsyncClient,
) -> None:
    """GET /api/v1/import_jobs/<id> path was never registered → FastAPI
    returns 405 (because PUT/PATCH/POST/DELETE *are* registered on it; FastAPI
    treats GET as method-not-allowed when other methods exist for that path).

    The legitimate read path GET /api/v1/import/jobs/<id> (note slash) still
    works — that's the only client read entrypoint.
    """
    create = await client_a.post(
        '/api/v1/import/jobs', json={'file_size': 1024},
    )
    assert create.status_code == 201, create.text
    job_id = create.json()['job_id']

    # GET on the underscore-channel path → 405 (other methods registered)
    r_bad = await client_a.get(f'/api/v1/import_jobs/{job_id}')
    assert r_bad.status_code == 405, (
        f'GET on read-only channel path expected 405, '
        f'got {r_bad.status_code}: {r_bad.text}'
    )

    # GET on the slash business path → 200 with full snapshot
    r_good = await client_a.get(f'/api/v1/import/jobs/{job_id}')
    assert r_good.status_code == 200, r_good.text
    snap = r_good.json()
    assert snap['job_id'] == job_id
    assert snap['status'] in {'uploading', 'queued', 'parsing'}, snap


# ---- 7. SSE channel mirrors WS for import_jobs -----------------------------


async def test_sse_subscribe_import_jobs_works(
    user_a, client_a: httpx.AsyncClient,
) -> None:
    """SSE channel must support import_jobs (mirror of stream.py TABLE_MODELS).
    Open SSE → drive a job → assert ≥1 event arrives with table='import_jobs'.
    """
    async with httpx.AsyncClient(base_url=BACKEND_URL, timeout=10.0) as sse_client:
        resp = await _open_sse(
            sse_client, user_a['access_token'],
            since=0, tables='import_jobs',
        )
        try:
            assert resp.status_code == 200, resp.text
            assert 'text/event-stream' in resp.headers.get('content-type', '')
            reader = SseReader(resp)

            # Initial replay-done on empty table.
            initial = await reader.read_events(expect=1, timeout=5.0)
            assert initial[0].get('event') == 'replay-done', initial
            assert initial[0]['data']['table'] == 'import_jobs', initial

            job_id = await _full_upload(client_a)

            # Collect events incrementally — `read_events(expect=N)` raises
            # on timeout AND loses already-buffered events (events list is
            # local to the call). So iterate small-batch reads, accumulate
            # across, and break when we see 'done'. Each call's timeout is
            # generous enough to span one phase, total budget caps total
            # wait. SseReader._buf is preserved between calls so partial
            # blocks survive.
            collected: list[dict[str, Any]] = []
            seen_done = False
            outer_deadline = time.monotonic() + 10.0
            while time.monotonic() < outer_deadline and not seen_done:
                remaining = max(0.5, outer_deadline - time.monotonic())
                try:
                    chunk = await reader.read_events(
                        expect=1, timeout=remaining,
                    )
                except asyncio.TimeoutError:
                    break
                collected.extend(chunk)
                for e in chunk:
                    if (
                        e.get('event') == 'event'
                        and e.get('data', {}).get('table') == 'import_jobs'
                        and e.get('data', {}).get('row', {}).get('data', {}).get('status') == 'done'
                    ):
                        seen_done = True
                        break
            import_jobs_events = [
                e for e in collected
                if e.get('event') == 'event'
                and e.get('data', {}).get('table') == 'import_jobs'
            ]
            assert import_jobs_events, (
                f'SSE got no import_jobs events; collected={collected}'
            )
            statuses = [
                e['data']['row']['data']['status']
                for e in import_jobs_events
            ]
            assert 'done' in statuses, (
                f'SSE never saw status=done; statuses={statuses} '
                f'(events={import_jobs_events})'
            )
            for e in import_jobs_events:
                assert e['data']['row']['data']['id'] == job_id, e
        finally:
            await resp.aclose()


# ---- 8. replay after reconnect picks up missed events ----------------------


async def test_replay_after_reconnect_picks_up_missed_events(
    user_a, client_a: httpx.AsyncClient, ws_connect,
) -> None:
    """Subscribe → drive complete → read first batch → note max rev →
    disconnect → wait for more progress → reconnect with `since=<max rev>` →
    confirm replay events all have rev > max rev (we caught up missed events,
    not the ones we already saw).
    """
    # First connection: capture an initial slice of events (queued/phase_b).
    async with ws_connect(user_a['access_token']) as ws1:
        await ws1.send(json.dumps(
            {'type': 'subscribe', 'table': 'import_jobs', 'since': 0}
        ))
        first = json.loads(await asyncio.wait_for(ws1.recv(), timeout=5.0))
        assert first['type'] == 'replay-done', first

        job_id = await _full_upload(client_a)

        # Read until we have ≥1 event but stop short of `done` so there
        # are events the next reconnect must replay.
        first_batch: list[dict[str, Any]] = []
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and len(first_batch) < 1:
            try:
                raw = await asyncio.wait_for(ws1.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            msg = json.loads(raw)
            if msg.get('type') == 'event' and msg.get('table') == 'import_jobs':
                first_batch.append(msg)
        assert first_batch, (
            'first connection saw no events before disconnect — race lost'
        )
        max_rev_seen = max(e['rev'] for e in first_batch)

    # ws1 is closed by the context manager. Wait for the worker to push the
    # job all the way to done (so additional events accumulate that we'll
    # replay on reconnect).
    async def _wait_done() -> None:
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            r = await client_a.get(f'/api/v1/import/jobs/{job_id}')
            if r.status_code == 200 and r.json().get('status') == 'done':
                return
            await asyncio.sleep(0.2)
        raise AssertionError(
            f'job {job_id} did not reach done before reconnect; '
            f'last status={r.json().get("status") if r.status_code == 200 else r.status_code}'
        )

    await _wait_done()

    # Reconnect with since=<max rev seen>. The replay path uses
    # `version > since`, so we should get every event that bumped version
    # after the last one ws1 saw.
    async with ws_connect(user_a['access_token']) as ws2:
        await ws2.send(json.dumps(
            {'type': 'subscribe', 'table': 'import_jobs', 'since': max_rev_seen}
        ))

        # Replay events until replay-done.
        replayed: list[dict[str, Any]] = []
        while True:
            raw = await asyncio.wait_for(ws2.recv(), timeout=5.0)
            msg = json.loads(raw)
            if msg.get('type') == 'replay-done':
                assert msg['table'] == 'import_jobs', msg
                # rev on replay-done = highest rev we've replayed (or the
                # `since` cursor if nothing).
                break
            assert msg.get('type') == 'event' and msg['table'] == 'import_jobs', msg
            replayed.append(msg)

        # Either we saw events and they're all > max_rev_seen, or nothing
        # missed. The interesting case is replayed-non-empty; assert it
        # explicitly because if first_batch reached done we wouldn't be testing
        # anything meaningful.
        if replayed:
            stale = [e for e in replayed if e['rev'] <= max_rev_seen]
            assert not stale, (
                f'replay returned events with rev <= since={max_rev_seen}; '
                f'stale={[(e["rev"], e["row"]["data"]["status"]) for e in stale]} '
                f'all={[(e["rev"], e["row"]["data"]["status"]) for e in replayed]}'
            )
            # And the final replayed event should be status='done' since we
            # waited for the worker to finish before reconnecting.
            final_status = replayed[-1]['row']['data']['status']
            assert final_status == 'done', (
                f'final replay event should be status=done; '
                f'got {final_status} (all={[e["row"]["data"]["status"] for e in replayed]})'
            )
        else:
            # If first_batch already contained the done event, this test
            # degenerates — call out the race. (In practice for the small
            # fixture, queued + phase_b come within ~50ms while done arrives
            # ~1-2s later, so first_batch=[queued or phase_b] is the typical
            # case and replayed contains the rest.)
            done_in_first = [
                e for e in first_batch
                if e['row']['data']['status'] == 'done'
            ]
            assert done_in_first, (
                'reconnect saw 0 replayed events but first_batch had no done; '
                f'first_batch statuses={[e["row"]["data"]["status"] for e in first_batch]} '
                f'max_rev_seen={max_rev_seen}'
            )
