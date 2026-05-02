"""Soak load generator for Stage 2 Step 6 上线把关.

Drives a steady stream of `PUT /api/v1/providers/<id>` writes against the
running test backend (9011) while holding open N WebSocket subscriptions on
the `providers` table for the same user. Records per-PUT latency so we can
emit a p95 number when the run ends.

Designed to be invoked from `soak.sh`, which handles env / RSS sampling /
SIGTERM-driven shutdown. This script speaks JSON-Lines on stdout — every
line is one event the bash side can `tail -f` and grep:

    {"type":"ready","user_id":"...","ws":5}
    {"type":"put","i":12,"rev":18,"latency_ms":7.4}
    {"type":"summary","puts":300,"errors":0,"p50_ms":...,"p95_ms":...,...}

Idempotent / interruptible: SIGTERM / SIGINT triggers a clean shutdown that
emits the summary line before exiting 0.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import signal
import sys
import time
from collections.abc import Iterable

import httpx
import websockets

BACKEND = os.environ.get('SOAK_BACKEND_URL', 'http://127.0.0.1:9011')
WS_URL = os.environ.get('SOAK_WS_URL', 'ws://127.0.0.1:9011/api/v1/stream')


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, separators=(',', ':')) + '\n')
    sys.stdout.flush()


async def register() -> dict:
    email = f'soak-{secrets.token_hex(4)}@example.com'
    async with httpx.AsyncClient(base_url=BACKEND, timeout=10.0) as c:
        r = await c.post(
            '/api/v1/auth/register',
            json={'email': email, 'password': 'soak-password-123'},
        )
        r.raise_for_status()
    body = r.json()
    return {
        'email': email,
        'access_token': body['access_token'],
        'user_id': body['user']['id'],
    }


async def hold_ws(token: str, idx: int, stop: asyncio.Event) -> None:
    """Keep one WS subscription open for the entire soak.

    Drains incoming frames so the broker queue (maxsize=200 per subscriber)
    never fills. Auto-reconnects if the server closes the socket — a 1 h
    soak has to survive transient reconnects without giving up.
    """
    while not stop.is_set():
        try:
            async with websockets.connect(
                WS_URL,
                subprotocols=[f'bearer.{token}'],
                ping_interval=20,
                ping_timeout=20,
                max_size=2**24,
            ) as ws:
                await ws.send(json.dumps({
                    'type': 'subscribe',
                    'table': 'providers',
                }))
                while not stop.is_set():
                    try:
                        await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    except websockets.ConnectionClosed:
                        break
        except Exception as e:
            emit({'type': 'ws_error', 'idx': idx, 'err': repr(e)})
            await asyncio.sleep(1.0)


async def put_loop(
    token: str,
    rate_hz: float,
    stop: asyncio.Event,
    latencies: list[float],
    errors: list[str],
) -> None:
    interval = 1.0 / rate_hz if rate_hz > 0 else 0.2
    headers = {'Authorization': f'Bearer {token}'}
    # Per-run prefix so concurrent / sequential soaks don't collide on a
    # `providers.id` already owned by a previous run's user. Backend rejects
    # PUT with 409 if (id) belongs to another user.
    run_prefix = f'soak-{secrets.token_hex(3)}'
    i = 0
    async with httpx.AsyncClient(
        base_url=BACKEND, timeout=10.0, headers=headers
    ) as c:
        while not stop.is_set():
            i += 1
            pid = f'{run_prefix}-{i % 50}'  # rotate 50 ids per run
            payload = {
                'kind': 'openai',
                'name': f'soak-{i}',
                'tick': i,
                'ts': time.time(),
            }
            t0 = time.perf_counter()
            try:
                r = await c.put(f'/api/v1/providers/{pid}', json=payload)
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                if r.status_code != 200:
                    errors.append(f'{r.status_code}:{r.text[:120]}')
                else:
                    latencies.append(elapsed_ms)
                    if i <= 10 or i % 50 == 0:
                        body = r.json()
                        emit({
                            'type': 'put',
                            'i': i,
                            'rev': body.get('version'),
                            'latency_ms': round(elapsed_ms, 2),
                        })
            except Exception as e:
                errors.append(repr(e))
                emit({'type': 'put_error', 'i': i, 'err': repr(e)})
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass


def percentile(samples: list[float], pct: float) -> float:
    if not samples:
        return 0.0
    s = sorted(samples)
    k = max(0, min(len(s) - 1, int(round(pct / 100.0 * (len(s) - 1)))))
    return s[k]


async def amain(args: argparse.Namespace) -> int:
    user = await register()
    token = user['access_token']
    stop = asyncio.Event()

    # Wire SIGTERM / SIGINT to set the stop event; the load gen loops will
    # observe it and tear down cleanly.
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows; not relevant here
            pass

    latencies: list[float] = []
    errors: list[str] = []

    ws_tasks = [
        asyncio.create_task(hold_ws(token, i, stop), name=f'ws-{i}')
        for i in range(args.ws_clients)
    ]
    put_task = asyncio.create_task(
        put_loop(token, args.rate_hz, stop, latencies, errors),
        name='put',
    )

    emit({
        'type': 'ready',
        'user_id': user['user_id'],
        'ws': args.ws_clients,
        'rate_hz': args.rate_hz,
        'duration_s': args.duration,
    })

    try:
        await asyncio.wait_for(stop.wait(), timeout=args.duration)
    except asyncio.TimeoutError:
        stop.set()

    # Give tasks ~3 s to wind down, then cancel.
    await asyncio.sleep(0.5)
    for t in [*ws_tasks, put_task]:
        if not t.done():
            t.cancel()
    await asyncio.gather(*ws_tasks, put_task, return_exceptions=True)

    summary = {
        'type': 'summary',
        'duration_s': args.duration,
        'ws_clients': args.ws_clients,
        'rate_hz': args.rate_hz,
        'puts': len(latencies),
        'errors': len(errors),
        'first_errors': errors[:5],
        'p50_ms': round(percentile(latencies, 50), 2),
        'p95_ms': round(percentile(latencies, 95), 2),
        'p99_ms': round(percentile(latencies, 99), 2),
        'avg_ms': round(sum(latencies) / len(latencies), 2) if latencies else 0.0,
        'max_ms': round(max(latencies), 2) if latencies else 0.0,
    }
    emit(summary)
    return 0


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--duration', type=int, default=3600,
                   help='soak duration seconds (default 3600 = 1h)')
    p.add_argument('--ws-clients', type=int, default=5,
                   help='concurrent WebSocket subscribers (default 5)')
    p.add_argument('--rate-hz', type=float, default=5.0,
                   help='target PUT rate per second (default 5)')
    args = p.parse_args()
    sys.exit(asyncio.run(amain(args)))
