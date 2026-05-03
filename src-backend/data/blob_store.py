"""BlobStore abstraction — Stage 4 硬前置 2 + Stage 4.5 Step 2 multipart.

Two implementations land in this batch:

- LocalFsBlobStore : default for dev/test. Writes bytes to
  `<root>/<sha256[:2]>/<sha256[2:]>` (2-char prefix shard avoids 100k+ file
  inode pressure on a single dir). presign_get_url() returns an HMAC-signed
  URL pointing back at FastAPI's GET /api/v1/blobs/{sha256}, so the same
  presign-and-fetch UX as S3 works without a real object store.

- S3BlobStore : skeleton for prod. Uses boto3 (lazy-imported so the dep stays
  optional in environments that only need LocalFS). Real flesh-out happens
  when Stage 4.5 needs it for ImportJob multipart uploads — for now it's
  enough to land the interface and prove LocalFS goes through it cleanly.

Stage 4.5 Step 2: 4 multipart methods extended onto the abstract base —
`create_multipart_upload` / `generate_part_url` / `complete_multipart_upload`
/ `abort_multipart_upload`. LocalFs simulates by:
  - upload_id = random uuid; per-part bytes land at
    `<root>/_multipart/<upload_id>/part_<n>` via a presigned-routed
    `PUT /api/v1/_internal/multipart/<upload_id>/part/<n>` endpoint
    (HMAC sig with same JWT_SECRET as blob presign).
  - complete concatenates parts in order, computes sha256, moves to
    `<sha[:2]>/<sha[2:]>` (the same canonical layout single-shot put uses),
    cleans up `_multipart/<upload_id>/`, returns final storage key.
  - abort just rm -rf the multipart dir.
S3BlobStore proxies to boto3 multipart API (skeleton; not exercised by
default LocalFs test profile).

Module-level state (LOCALFS_ROOT, BLOB_STORE_KIND, presign secret cache) is
read from env at first use, not at import time, so importing this module in
plain Python (e.g. for type hints, alembic env.py) doesn't fail on a missing
JWT_SECRET. The router that mounts this is itself behind the
BACKEND_DATA_API_ENABLED flag, which fail-fasts on JWT_SECRET absence.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import secrets as _secrets
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode


# ---- presign signing --------------------------------------------------------


def _presign_secret() -> str:
    """Use JWT_SECRET as the HMAC key — same trust boundary, one less knob.

    Read at call time so the module can import in environments without
    JWT_SECRET set (e.g. unit tests for unrelated code).
    """
    secret = os.environ.get('JWT_SECRET')
    if not secret:
        raise RuntimeError('JWT_SECRET required to sign blob URLs')
    return secret


def sign_blob_url(sha256: str, exp: int) -> str:
    """Return hex HMAC-SHA256 of `sha256|exp`.

    Keep the payload narrow (just sha256 + exp) so URLs are stable and short.
    No user_id binding: the bytes are content-addressed and any user with a
    `blob_refs` row can issue presigns; treating the URL as a bearer token for
    that specific blob (not for any data the user owns) matches how S3
    presigning works.
    """
    msg = f'{sha256}|{exp}'.encode('utf-8')
    return hmac.new(
        _presign_secret().encode('utf-8'), msg, hashlib.sha256
    ).hexdigest()


def verify_blob_signature(sha256: str, exp: int, sig: str) -> bool:
    expected = sign_blob_url(sha256, exp)
    # constant-time compare so timing doesn't leak the secret
    return hmac.compare_digest(expected, sig)


# ---- multipart presign signing ---------------------------------------------
#
# Stage 4.5 Step 2: LocalFs simulates S3 multipart by routing per-part PUTs
# back to a FastAPI internal endpoint. We sign `<upload_id>|<part_number>|exp`
# so the endpoint can verify the URL hasn't been forged or extended past TTL.
# The signature scheme is parallel to sign_blob_url — same HMAC key, separate
# message namespace so a leaked blob URL can't be reused to PUT a part (and
# vice versa).


def sign_multipart_part_url(upload_id: str, part_number: int, exp: int) -> str:
    msg = f'mp|{upload_id}|{part_number}|{exp}'.encode('utf-8')
    return hmac.new(
        _presign_secret().encode('utf-8'), msg, hashlib.sha256
    ).hexdigest()


def verify_multipart_part_signature(
    upload_id: str, part_number: int, exp: int, sig: str
) -> bool:
    expected = sign_multipart_part_url(upload_id, part_number, exp)
    return hmac.compare_digest(expected, sig)


# ---- abstract interface -----------------------------------------------------


class BlobStore(ABC):
    @abstractmethod
    async def put(
        self, sha256: str, data: bytes, content_type: str
    ) -> str:
        """Persist `data` under a key derived from `sha256`. Idempotent: a
        second put with the same sha256 is a no-op (we trust the hash).
        Returns the storage_key to record in the `blobs` row.
        """

    @abstractmethod
    async def get(self, storage_key: str) -> bytes:
        """Fetch the entire blob. Raises FileNotFoundError if absent."""

    def open_stream(
        self, storage_key: str, *, chunk_size: int = 1024 * 1024
    ) -> AsyncIterator[bytes]:
        """Stream the blob in chunks. Default impl falls back to `get()`
        followed by manual chunking — fine for tiny in-memory blobs but
        wasteful for large ones. LocalFsBlobStore / S3BlobStore override this
        with real streaming reads. Stage 4.5 Phase A relies on this so a
        200MB raw upload can be ijson-parsed without loading the whole file
        in RAM.

        Note: declared `def` (not `async def`) returning an async generator —
        callers do `async for chunk in store.open_stream(key): ...`, no
        intermediate `await store.open_stream(...)` needed.
        """
        async def _iter() -> AsyncIterator[bytes]:
            data = await self.get(storage_key)
            for i in range(0, len(data), chunk_size):
                yield data[i:i + chunk_size]

        return _iter()

    @abstractmethod
    async def exists(self, storage_key: str) -> bool: ...

    @abstractmethod
    async def delete(self, storage_key: str) -> None:
        """Remove the bytes. No-op if already gone (idempotent)."""

    @abstractmethod
    def presign_get_url(
        self, sha256: str, *, ttl_seconds: int, base_url: str
    ) -> str:
        """Return an absolute URL the client can GET without the bearer.

        For LocalFS the URL points back at our /api/v1/blobs/{sha256} endpoint
        with HMAC sig + exp. For S3 it'll be a real S3 presigned URL.
        """

    # ---- Stage 4.5 Step 2 multipart upload protocol ------------------------
    #
    # The 4 methods below mirror S3's multipart upload API. Stage 4.5 ImportJob
    # uploads a 200MB+ raw export by splitting client-side into ≤ 8MB parts and
    # PUT'ing each to a presigned URL. `complete_multipart_upload` assembles
    # them server-side and returns the final canonical storage_key.
    #
    # part_number is 1-indexed (S3 convention). `parts` passed to complete must
    # be a list of `{part_number, etag}` dicts in part_number order, contiguous
    # 1..N — the router enforces this contract before calling complete.

    @abstractmethod
    async def create_multipart_upload(self, key: str) -> str:
        """Begin a multipart upload at `key` (e.g. `imports/<user>/<job>.json`).
        Returns an opaque `multipart_upload_id` the caller stores on the
        ImportJob row and threads through generate_part_url / complete / abort.
        Idempotency: not guaranteed — callers should only call once per logical
        upload; create-then-abort is the documented retry path.
        """

    @abstractmethod
    async def generate_part_url(
        self,
        multipart_upload_id: str,
        part_number: int,
        *,
        ttl_seconds: int,
        base_url: str,
    ) -> str:
        """Return a presigned PUT URL the client uploads `part_number`'s bytes
        to. TTL bounds replay window. For LocalFs this routes back to our
        internal endpoint; for S3 it's a real S3 presigned URL bound to the
        upload_id + part_number.
        """

    @abstractmethod
    async def complete_multipart_upload(
        self,
        multipart_upload_id: str,
        parts: list[dict],
        key: str,
    ) -> str:
        """Assemble previously-uploaded parts and finalize. `parts` is the
        client-claimed `[{part_number, etag}, ...]` list (router validates
        contiguous + 1-indexed before passing in). Returns the final
        storage_key (canonical content-addressed location for LocalFs; the
        original `key` for S3 since S3 keys are caller-chosen).
        """

    @abstractmethod
    async def abort_multipart_upload(self, multipart_upload_id: str) -> None:
        """Discard any uploaded parts and the upload session itself. Idempotent
        — calling on an already-aborted / non-existent upload_id is a no-op so
        the DELETE /api/v1/import/jobs/{id} endpoint can be safely retried.
        """


# ---- LocalFS implementation -------------------------------------------------


def _localfs_root() -> Path:
    """Read root path from env at call time. Defaults to repo-relative
    src-backend/.blob-store so dev / tests don't need extra config."""
    raw = os.environ.get('BLOB_STORE_PATH')
    if raw:
        return Path(raw).resolve()
    # __file__ = .../src-backend/data/blob_store.py
    return Path(__file__).resolve().parent.parent / '.blob-store'


def _path_for(sha256: str, root: Path) -> Path:
    """`<root>/<sha[:2]>/<sha[2:]>` — 2-char prefix shard. 256 directories, even
    spread across all sha256 inputs."""
    if len(sha256) != 64 or any(c not in '0123456789abcdef' for c in sha256):
        raise ValueError(f'invalid sha256 hex: {sha256!r}')
    return root / sha256[:2] / sha256[2:]


class LocalFsBlobStore(BlobStore):
    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = root or _localfs_root()
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, storage_key: str) -> Path:
        # storage_key == sha256 in LocalFS (we use sha256 as the key directly).
        return _path_for(storage_key, self.root)

    async def put(
        self, sha256: str, data: bytes, content_type: str
    ) -> str:
        del content_type  # LocalFS doesn't store metadata; PG row owns it
        path = _path_for(sha256, self.root)

        def _write() -> None:
            if path.exists():
                # Trust the hash — same sha256 means same bytes.
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write: tmp + rename. Otherwise an interrupted write could
            # leave a half-file at the canonical path that future readers
            # mistake for present-and-correct.
            tmp = path.parent / f'.tmp-{_secrets.token_hex(8)}'
            try:
                tmp.write_bytes(data)
                tmp.replace(path)
            finally:
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except FileNotFoundError:
                        pass

        await asyncio.to_thread(_write)
        return sha256

    async def get(self, storage_key: str) -> bytes:
        path = self._resolve(storage_key)

        def _read() -> bytes:
            try:
                return path.read_bytes()
            except FileNotFoundError as e:
                raise FileNotFoundError(
                    f'blob bytes missing on disk: {storage_key}'
                ) from e

        return await asyncio.to_thread(_read)

    def open_stream(
        self, storage_key: str, *, chunk_size: int = 1024 * 1024
    ) -> AsyncIterator[bytes]:
        """LocalFS streaming read: open() the file once and yield chunks via
        asyncio.to_thread per read. Avoids the default impl's read-whole-blob
        path so Phase A can ijson-parse a 200MB upload with bounded RSS.
        """
        path = self._resolve(storage_key)

        async def _iter() -> AsyncIterator[bytes]:
            if not await asyncio.to_thread(path.exists):
                raise FileNotFoundError(
                    f'blob bytes missing on disk: {storage_key}'
                )
            # Run blocking file IO on a thread; yield control between chunks
            # so the worker's event loop can service WS pings / progress
            # publishes during a long parse.
            f = await asyncio.to_thread(open, path, 'rb')
            try:
                while True:
                    chunk = await asyncio.to_thread(f.read, chunk_size)
                    if not chunk:
                        break
                    yield chunk
            finally:
                await asyncio.to_thread(f.close)

        return _iter()

    async def exists(self, storage_key: str) -> bool:
        path = self._resolve(storage_key)
        return await asyncio.to_thread(path.exists)

    async def delete(self, storage_key: str) -> None:
        path = self._resolve(storage_key)

        def _unlink() -> None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

        await asyncio.to_thread(_unlink)

    def presign_get_url(
        self, sha256: str, *, ttl_seconds: int, base_url: str
    ) -> str:
        import time
        exp = int(time.time()) + ttl_seconds
        sig = sign_blob_url(sha256, exp)
        qs = urlencode({'exp': exp, 'sig': sig})
        # base_url is the absolute backend origin (e.g. http://127.0.0.1:9011).
        # Returning an absolute URL lets the frontend embed it in <img src>
        # cross-origin without further plumbing.
        return f'{base_url.rstrip("/")}/api/v1/blobs/{sha256}/data?{qs}'

    # ---- multipart simulation ---------------------------------------------
    #
    # Layout: `<root>/_multipart/<upload_id>/part_<n>` for staged parts +
    # `<root>/_multipart/<upload_id>/.meta.json` for the original `key` so
    # complete can know where to put the assembled blob if a caller wants the
    # original key (LocalFs ignores it and uses sha256-addressed canonical key,
    # but we record it for parity with S3/debugging).

    def _multipart_dir(self, upload_id: str) -> Path:
        # Strip any leading underscores / slashes — we control the value
        # (uuid4 hex), so this is defense-in-depth rather than untrusted input.
        if not upload_id or '/' in upload_id or '..' in upload_id:
            raise ValueError(f'invalid multipart upload_id: {upload_id!r}')
        return self.root / '_multipart' / upload_id

    def _part_path(self, upload_id: str, part_number: int) -> Path:
        if part_number < 1:
            raise ValueError(f'part_number must be >= 1, got {part_number}')
        return self._multipart_dir(upload_id) / f'part_{part_number}'

    async def create_multipart_upload(self, key: str) -> str:
        upload_id = _secrets.token_hex(16)
        dirp = self._multipart_dir(upload_id)

        def _mk() -> None:
            dirp.mkdir(parents=True, exist_ok=True)
            # Stash original requested key so callers/debug can correlate.
            (dirp / '.meta.json').write_text(
                f'{{"key": {key!r}}}', encoding='utf-8'
            )

        await asyncio.to_thread(_mk)
        return upload_id

    async def generate_part_url(
        self,
        multipart_upload_id: str,
        part_number: int,
        *,
        ttl_seconds: int,
        base_url: str,
    ) -> str:
        import time
        exp = int(time.time()) + ttl_seconds
        sig = sign_multipart_part_url(multipart_upload_id, part_number, exp)
        qs = urlencode({'exp': exp, 'sig': sig})
        return (
            f'{base_url.rstrip("/")}/api/v1/_internal/multipart/'
            f'{multipart_upload_id}/part/{part_number}?{qs}'
        )

    async def write_part(
        self, multipart_upload_id: str, part_number: int, data: bytes
    ) -> str:
        """Internal helper invoked by the routed PUT endpoint after the HMAC
        signature has been verified. Writes part bytes atomically and returns
        the etag (sha256 of the part bytes — same trust boundary as S3, where
        etag is also a content hash for non-multipart parts).
        """
        path = self._part_path(multipart_upload_id, part_number)

        def _write() -> str:
            if not path.parent.exists():
                raise FileNotFoundError(
                    f'multipart upload {multipart_upload_id} does not exist'
                )
            tmp = path.parent / f'.tmp-part-{_secrets.token_hex(8)}'
            try:
                tmp.write_bytes(data)
                tmp.replace(path)
            finally:
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except FileNotFoundError:
                        pass
            return hashlib.sha256(data).hexdigest()

        return await asyncio.to_thread(_write)

    async def complete_multipart_upload(
        self,
        multipart_upload_id: str,
        parts: list[dict],
        key: str,
    ) -> str:
        del key  # LocalFs uses sha256-addressed canonical key (parity with put)
        dirp = self._multipart_dir(multipart_upload_id)

        def _assemble() -> str:
            if not dirp.exists():
                raise FileNotFoundError(
                    f'multipart upload {multipart_upload_id} does not exist'
                )
            # Validate every claimed part actually exists on disk before we
            # start assembling. A missing part means the client claimed an
            # etag for bytes we never received — abort with a clear error so
            # the router can return 400 instead of producing a corrupt blob.
            ordered = sorted(parts, key=lambda p: int(p['part_number']))
            sources: list[Path] = []
            for p in ordered:
                pp = self._part_path(
                    multipart_upload_id, int(p['part_number'])
                )
                if not pp.exists():
                    raise FileNotFoundError(
                        f'part {p["part_number"]} not uploaded'
                    )
                sources.append(pp)

            # Stream parts through sha256 + a tmp file so we know the canonical
            # destination before moving. Avoids loading all parts in RAM (a
            # 200MB import = ~25 parts × 8MB).
            h = hashlib.sha256()
            staging = dirp / '.assembled'
            with open(staging, 'wb') as out:
                for src in sources:
                    with open(src, 'rb') as f:
                        while True:
                            chunk = f.read(1024 * 1024)
                            if not chunk:
                                break
                            h.update(chunk)
                            out.write(chunk)
            sha256 = h.hexdigest()
            final = _path_for(sha256, self.root)
            final.parent.mkdir(parents=True, exist_ok=True)
            # Trust-the-hash dedup: if the canonical path already holds bytes,
            # we don't overwrite — same sha256 means same bytes.
            if final.exists():
                staging.unlink()
            else:
                staging.replace(final)
            # Cleanup the multipart staging dir so abort/complete leave no trace.
            import shutil
            shutil.rmtree(dirp, ignore_errors=True)
            return sha256

        return await asyncio.to_thread(_assemble)

    async def abort_multipart_upload(self, multipart_upload_id: str) -> None:
        dirp = self._multipart_dir(multipart_upload_id)

        def _rm() -> None:
            import shutil
            shutil.rmtree(dirp, ignore_errors=True)

        await asyncio.to_thread(_rm)


# ---- S3 implementation (skeleton) -------------------------------------------


class S3BlobStore(BlobStore):
    """boto3-backed implementation. Loaded lazily so non-S3 deployments don't
    pay for the dep. The full multipart suite lands in Stage 4.5; this Stage 4
    硬前置 2 batch ships only single-shot put/get and a real presigned URL.
    """

    def __init__(
        self,
        bucket: str,
        endpoint: Optional[str],
        access_key: str,
        secret_key: str,
        region: Optional[str] = None,
        prefix: str = 'blobs/',
    ) -> None:
        try:
            import boto3  # type: ignore
        except ImportError as e:  # pragma: no cover — only hit when S3 selected
            raise RuntimeError(
                'BLOB_STORE_KIND=s3 requires boto3. '
                'Install with: pip install boto3'
            ) from e
        self._boto3 = boto3
        self.bucket = bucket
        self.prefix = prefix.rstrip('/') + '/'
        self._client = boto3.client(
            's3',
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
        )

    def _key(self, sha256: str) -> str:
        return f'{self.prefix}{sha256[:2]}/{sha256[2:]}'

    async def put(
        self, sha256: str, data: bytes, content_type: str
    ) -> str:
        key = self._key(sha256)

        def _put() -> None:
            # Skip if exists. S3's HeadObject is the cheapest way to ask.
            try:
                self._client.head_object(Bucket=self.bucket, Key=key)
                return
            except self._client.exceptions.ClientError as e:
                if e.response.get('Error', {}).get('Code') not in (
                    '404', 'NoSuchKey', 'NotFound'
                ):
                    raise
            self._client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
            )

        await asyncio.to_thread(_put)
        return key

    async def get(self, storage_key: str) -> bytes:
        def _get() -> bytes:
            obj = self._client.get_object(Bucket=self.bucket, Key=storage_key)
            return obj['Body'].read()

        return await asyncio.to_thread(_get)

    async def exists(self, storage_key: str) -> bool:
        def _head() -> bool:
            try:
                self._client.head_object(Bucket=self.bucket, Key=storage_key)
                return True
            except self._client.exceptions.ClientError:
                return False

        return await asyncio.to_thread(_head)

    async def delete(self, storage_key: str) -> None:
        def _del() -> None:
            self._client.delete_object(Bucket=self.bucket, Key=storage_key)

        await asyncio.to_thread(_del)

    def presign_get_url(
        self, sha256: str, *, ttl_seconds: int, base_url: str
    ) -> str:
        del base_url  # S3 returns its own host
        return self._client.generate_presigned_url(
            'get_object',
            Params={'Bucket': self.bucket, 'Key': self._key(sha256)},
            ExpiresIn=ttl_seconds,
        )

    # ---- Stage 4.5 Step 2 multipart (skeleton; not exercised in default
    # LocalFs test profile, but the abstract base requires implementations) ---

    async def create_multipart_upload(self, key: str) -> str:
        def _create() -> str:
            resp = self._client.create_multipart_upload(
                Bucket=self.bucket,
                Key=key,
            )
            return resp['UploadId']

        return await asyncio.to_thread(_create)

    async def generate_part_url(
        self,
        multipart_upload_id: str,
        part_number: int,
        *,
        ttl_seconds: int,
        base_url: str,
    ) -> str:
        del base_url  # S3 returns its own host
        # Note: S3 presigned URL for upload_part requires us to know the Key
        # (object name). Caller must thread this through; we stash via meta?
        # For Stage 4.5 we accept that callers pass the Key separately when
        # the S3 backend is selected. The concrete plumbing path is:
        # imports router knows job.raw_object_key and would call a richer
        # variant. LocalFs is the default & fully wired path — this skeleton
        # is documented-incomplete on purpose.
        raise NotImplementedError(
            'S3BlobStore.generate_part_url requires Key plumbing; '
            'LocalFs is the default Stage 4.5 backend'
        )

    async def complete_multipart_upload(
        self,
        multipart_upload_id: str,
        parts: list[dict],
        key: str,
    ) -> str:
        def _complete() -> str:
            self._client.complete_multipart_upload(
                Bucket=self.bucket,
                Key=key,
                UploadId=multipart_upload_id,
                MultipartUpload={
                    'Parts': [
                        {
                            'PartNumber': int(p['part_number']),
                            'ETag': p['etag'],
                        }
                        for p in sorted(parts, key=lambda p: int(p['part_number']))
                    ],
                },
            )
            return key

        return await asyncio.to_thread(_complete)

    async def abort_multipart_upload(self, multipart_upload_id: str) -> None:
        def _abort() -> None:
            # Without the original Key we can't issue AbortMultipartUpload on
            # S3 — same plumbing gap as generate_part_url. Stage 4.5 default
            # path is LocalFs; S3 wiring lands when prod target is picked.
            raise NotImplementedError(
                'S3BlobStore.abort_multipart_upload requires Key plumbing'
            )

        await asyncio.to_thread(_abort)


# ---- factory ----------------------------------------------------------------


_blob_store_singleton: Optional[BlobStore] = None


def get_blob_store() -> BlobStore:
    """Process-wide singleton. Selected by BLOB_STORE_KIND env (default
    `local-fs`). For tests we want one instance per process so file paths
    stay stable; FastAPI dependency-injects this lazily on first request.
    """
    global _blob_store_singleton
    if _blob_store_singleton is not None:
        return _blob_store_singleton

    kind = os.environ.get('BLOB_STORE_KIND', 'local-fs').strip().lower()
    if kind in ('local-fs', 'local', 'localfs', ''):
        _blob_store_singleton = LocalFsBlobStore()
        return _blob_store_singleton
    if kind == 's3':
        bucket = os.environ.get('BLOB_STORE_BUCKET')
        endpoint = os.environ.get('BLOB_STORE_ENDPOINT') or None
        access_key = os.environ.get('BLOB_STORE_ACCESS_KEY')
        secret_key = os.environ.get('BLOB_STORE_SECRET_KEY')
        region = os.environ.get('BLOB_STORE_REGION') or None
        missing = [
            n for n, v in (
                ('BLOB_STORE_BUCKET', bucket),
                ('BLOB_STORE_ACCESS_KEY', access_key),
                ('BLOB_STORE_SECRET_KEY', secret_key),
            ) if not v
        ]
        if missing:
            raise RuntimeError(
                'BLOB_STORE_KIND=s3 missing env vars: ' + ', '.join(missing)
            )
        # mypy: above check guarantees these are non-None strings
        assert bucket and access_key and secret_key
        _blob_store_singleton = S3BlobStore(
            bucket=bucket,
            endpoint=endpoint,
            access_key=access_key,
            secret_key=secret_key,
            region=region,
        )
        return _blob_store_singleton
    raise RuntimeError(f'unknown BLOB_STORE_KIND: {kind!r}')


def reset_blob_store_for_tests() -> None:
    """Tests may swap BLOB_STORE_PATH between runs; force a re-instantiation.
    Production code never calls this.
    """
    global _blob_store_singleton
    _blob_store_singleton = None


# ---- common config ----------------------------------------------------------

# Frontend-aligned threshold. Below = inline base64 in the row, above = upload
# to BlobStore + store {type:'ref', ...} in the row. Stage 4 主体批次会读这个
# 决定 attachment 序列化路径。
BLOB_INLINE_MAX_BYTES = int(
    os.environ.get('BLOB_INLINE_MAX_BYTES', str(64 * 1024))
)

# Hard cap on a single uploaded blob. 100MB = comfortably above the
# MAX_MESSAGE_FILE_SIZE_MB=20 ceiling but low enough to bound a single bad
# request. Stage 4.5 will introduce multipart for genuinely large files.
BLOB_MAX_UPLOAD_BYTES = int(
    os.environ.get('BLOB_MAX_UPLOAD_BYTES', str(100 * 1024 * 1024))
)

BLOB_PRESIGN_TTL_SECONDS = int(
    os.environ.get('BLOB_PRESIGN_TTL_SECONDS', '3600')
)
