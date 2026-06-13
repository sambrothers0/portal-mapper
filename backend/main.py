from __future__ import annotations

import asyncio
import os
import zipfile
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager
from pathlib import PurePosixPath

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.gzip import GZipMiddleware

from parser import normalize_block_type, scan_region_bytes

DEFAULT_BLOCK_TYPE = 'minecraft:nether_portal'
DEFAULT_DIMENSION = 'overworld'
MAX_MATCHES = 100_000

# How many uploads may be parsed at once. Each in-flight scan holds one
# dimension's region bytes resident (up to a few GB for a 4 GB save) and drives
# the whole process pool, so we serialize by default: one scan at a time gets
# every core (fastest individual completion — speed is the priority) and the box
# can never be pushed into swap/OOM by concurrent big uploads. Tune via env on a
# bigger host. ``locked()`` treats "all permits taken" as busy, so any value works.
MAX_CONCURRENT_SCANS = max(1, int(os.environ.get('MAX_CONCURRENT_SCANS', '1')))

# Origins allowed to call the API. Defaults to open ('*') for local dev; in prod
# set ALLOWED_ORIGINS to the frontend's exact origin (comma-separated for more
# than one) so the public API only answers the real site.
_origins_env = os.environ.get('ALLOWED_ORIGINS', '*').strip()
ALLOWED_ORIGINS = (
    ['*'] if _origins_env == '*' else [o.strip() for o in _origins_env.split(',') if o.strip()]
)

# A save keeps each dimension's block data in its own ``region`` directory. The
# dimension is identified by the directory that *contains* that ``region`` dir:
# the Nether lives under ``DIM-1``, the End under ``DIM1``, and the Overworld's
# ``region`` sits directly in the world root (no special parent).
_DIMENSION_BY_PARENT = {'DIM-1': 'nether', 'DIM1': 'end'}
VALID_DIMENSIONS = {'overworld', 'nether', 'end'}

# Tiered upload ceilings. Anyone may upload up to the public limit; holders of a
# valid access code may upload up to the full limit. The limit is checked against
# the *uploaded* zip (the browser pre-filters the save to one dimension first), so
# the frontend gates on the filtered blob with these same numbers.
_MIB = 1024 ** 2
_GIB = 1024 ** 3
PUBLIC_MAX_UPLOAD_BYTES = 250 * _MIB
FULL_MAX_UPLOAD_BYTES = 4 * _GIB
# A multipart body is a little larger than the file (boundary + part headers), so
# allow a small margin over each advertised limit before bouncing a genuine file.
_MULTIPART_MARGIN = 2 * _MIB
PUBLIC_UPLOAD_LIMIT_BYTES = PUBLIC_MAX_UPLOAD_BYTES + _MULTIPART_MARGIN
FULL_UPLOAD_LIMIT_BYTES = FULL_MAX_UPLOAD_BYTES + _MULTIPART_MARGIN

# Ceiling on the *decompressed* region bytes read out of an upload, regardless of
# tier. The upload-size guard only sees the compressed Content-Length, so a small
# zip whose members inflate to many GB (a "zip bomb") would otherwise OOM the box
# during extraction. A legitimate save's region data is <= the 4 GB product limit
# by definition, so this never rejects a real upload while capping RAM at ~4 GB.
MAX_DECOMPRESSED_REGION_BYTES = FULL_MAX_UPLOAD_BYTES + _MULTIPART_MARGIN
_READ_BLOCK_BYTES = 1 << 20

# Secret codes that unlock the full (4 GB) limit. Comma-separated in the env.
# Hand a code to someone to grant access; delete it (and restart) to revoke. The
# client sends its code in the ``X-Access-Key`` header. Empty by default → nobody
# has full access until you set this, so set it before relying on your own code.
FULL_ACCESS_KEYS = frozenset(
    k.strip() for k in os.environ.get('FULL_ACCESS_KEYS', '').split(',') if k.strip()
)
ACCESS_HEADER = 'x-access-key'


def _upload_limit_for(request: Request) -> int:
    """The Content-Length ceiling for this request, by access tier."""
    key = request.headers.get(ACCESS_HEADER, '').strip()
    if key and key in FULL_ACCESS_KEYS:
        return FULL_UPLOAD_LIMIT_BYTES
    return PUBLIC_UPLOAD_LIMIT_BYTES


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One pool reused for the life of the process. Region-file parsing is
    # CPU-bound pure Python, so we spread it across every core; a process pool
    # (not threads) is what actually sidesteps the GIL.
    executor = ProcessPoolExecutor(max_workers=os.cpu_count() or 1)
    app.state.executor = executor
    # Gate on concurrent scans so a burst of uploads can't exhaust RAM/disk. Made
    # in the lifespan so it binds to the running event loop.
    app.state.scan_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SCANS)
    try:
        yield
    finally:
        executor.shutdown(cancel_futures=True)


app = FastAPI(lifespan=lifespan)


@app.middleware('http')
async def enforce_upload_limit(request: Request, call_next):
    """Reject oversized uploads on their headers, before the body is parsed.

    This runs ahead of the endpoint, so an over-limit upload is refused from its
    ``Content-Length`` alone — the multi-GB body is never streamed into the temp
    dir. The ceiling is the caller's access tier (public vs. full-access code).
    """
    if request.method == 'POST' and request.url.path == '/parse-blocks':
        limit = _upload_limit_for(request)
        content_length = request.headers.get('content-length')
        if content_length and content_length.isdigit() and int(content_length) > limit:
            if limit == FULL_UPLOAD_LIMIT_BYTES:
                detail = 'Upload exceeds the 4 GB limit.'
            else:
                detail = (
                    f'Upload exceeds the {PUBLIC_MAX_UPLOAD_BYTES // _MIB} MB limit for '
                    'visitors. Enter an access code to upload up to 4 GB.'
                )
            return JSONResponse(status_code=413, content={'detail': detail})
    return await call_next(request)


# Order matters: the middleware added last is the outermost. CORS must be
# outermost so its headers are attached even to the 413 short-circuited above
# (and so it can answer the preflight for the custom X-Access-Key header).
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=['*'],
    allow_headers=['*'],
)


def _region_dimension(name: str) -> str | None:
    """Return which dimension a region-file member belongs to, or ``None``.

    A save keeps block data in directories literally named ``region`` (the
    Overworld at ``<world>/region``, the Nether at ``<world>/DIM-1/region``,
    the End at ``<world>/DIM1/region``). Everything else in the archive
    (``entities``, ``poi``, ``playerdata``, ``level.dat``, ...) is irrelevant to
    a block search and can be gigabytes, so it is never extracted.

    Members that aren't region files (or aren't inside a ``region`` dir) yield
    ``None``; otherwise the dimension is derived from the parent directory.
    """
    if not (name.endswith('.mca') or name.endswith('.mcc')):
        return None
    parts = PurePosixPath(name).parts
    if len(parts) < 2 or parts[-2] != 'region':
        return None
    parent = parts[-3] if len(parts) >= 3 else ''
    return _DIMENSION_BY_PARENT.get(parent, 'overworld')


class _ArchiveTooLarge(Exception):
    """A member's decompressed size pushed total region bytes past the ceiling."""


def _read_member_capped(archive: zipfile.ZipFile, info: zipfile.ZipInfo, remaining: int) -> bytes:
    """Decompress one zip member, aborting if it would exceed ``remaining`` bytes.

    Reads the stream incrementally rather than trusting the uncompressed size in
    the central directory (which a crafted archive can understate), so a zip bomb
    is stopped while inflating instead of after.
    """
    out = bytearray()
    with archive.open(info) as handle:
        while True:
            block = handle.read(_READ_BLOCK_BYTES)
            if not block:
                break
            out += block
            if len(out) > remaining:
                raise _ArchiveTooLarge
    return bytes(out)


def _read_region_work(spooled_file, dimension: str) -> list[tuple[bytes, dict[str, bytes]]]:
    """Read one dimension's region files straight from the upload zip into memory.

    Returns a work unit ``(mca_bytes, mcc_map)`` per ``.mca`` member, ready to
    fan across the process pool. We never extract to a temp directory — that
    round-trip (write ~1 GiB out, read it back in the workers) is pure I/O and
    is especially slow on network-attached block storage.

    ``.mcc`` oversized-chunk sidecars share the dimension's single ``region``
    directory and are keyed by basename; the same (usually empty) map is handed
    to every worker, since a sidecar is only consulted when a chunk references
    it. Reads straight from Starlette's already-on-disk spooled upload, so the
    full 4 GiB archive is never held in memory at once.
    """
    spooled_file.seek(0)
    with zipfile.ZipFile(spooled_file) as archive:
        mca_members = []
        mcc_members: dict[str, zipfile.ZipInfo] = {}
        for info in archive.infolist():
            if _region_dimension(info.filename) != dimension:
                continue
            name = PurePosixPath(info.filename).name
            if name.endswith('.mca'):
                mca_members.append(info)
            else:  # .mcc sidecar
                mcc_members[name] = info

        if not mca_members:
            raise FileNotFoundError(
                f'No region files found for the {dimension}. '
                'Is this a valid Minecraft save, and has that dimension been visited?'
            )

        # Read every member through the cap, decrementing a shared budget so the
        # *total* decompressed region data can't exceed the ceiling either.
        budget = MAX_DECOMPRESSED_REGION_BYTES
        mcc_map: dict[str, bytes] = {}
        for name, info in mcc_members.items():
            data = _read_member_capped(archive, info, budget)
            budget -= len(data)
            mcc_map[name] = data

        work: list[tuple[bytes, dict[str, bytes]]] = []
        for info in mca_members:
            data = _read_member_capped(archive, info, budget)
            budget -= len(data)
            work.append((data, mcc_map))
        return work


@app.post('/parse-blocks')
async def parse_blocks(
    request: Request,
    file: UploadFile = File(...),
    block_type: str = Form(default=DEFAULT_BLOCK_TYPE),
    dimension: str = Form(default=DEFAULT_DIMENSION),
):
    if not file.filename or not file.filename.endswith('.zip'):
        raise HTTPException(status_code=400, detail='Upload must be a .zip of the save folder')

    if dimension not in VALID_DIMENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f'dimension must be one of {sorted(VALID_DIMENSIONS)}',
        )

    # The upload-size ceiling (tiered by access code) is enforced up front in the
    # enforce_upload_limit middleware, before the body is ever read.

    loop = asyncio.get_running_loop()
    executor: ProcessPoolExecutor = request.app.state.executor
    semaphore: asyncio.Semaphore = request.app.state.scan_semaphore

    # Refuse new work the moment we're at capacity rather than queueing uploads
    # that would pile their region bytes into RAM behind the running scan. Fail
    # fast with 503 so the client can simply retry — the connection isn't held.
    if semaphore.locked():
        raise HTTPException(
            status_code=503,
            detail='The server is busy scanning another world. Please try again in a moment.',
            headers={'Retry-After': '30'},
        )

    async with semaphore:
        # Reading the region members out of the zip is blocking disk/zlib work; run
        # it off the event loop. The bytes go straight to the pool — no temp dir.
        try:
            work = await loop.run_in_executor(None, _read_region_work, file.file, dimension)
        except zipfile.BadZipFile as exc:
            raise HTTPException(status_code=400, detail='File is not a valid zip archive') from exc
        except _ArchiveTooLarge as exc:
            raise HTTPException(
                status_code=413,
                detail='The zip expands to far more data than a real save — refused.',
            ) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        # Fan every region file out across the pool and await them together. Each
        # worker caps its own coordinate list at MAX_MATCHES (so no single region
        # can build a huge list) and returns the true per-file count alongside.
        scans = [
            loop.run_in_executor(executor, scan_region_bytes, mca_bytes, mcc_map, block_type, MAX_MATCHES)
            for mca_bytes, mcc_map in work
        ]
        per_file = await asyncio.gather(*scans)

    # Sum the true totals, but stop accumulating coordinates at the cap.
    count = 0
    matches: list[dict[str, int]] = []
    for file_count, file_matches in per_file:
        count += file_count
        if len(matches) < MAX_MATCHES:
            matches.extend(file_matches[: MAX_MATCHES - len(matches)])
    truncated = count > MAX_MATCHES

    return {
        'block_type': normalize_block_type(block_type),
        'dimension': dimension,
        'count': count,
        'truncated': truncated,
        'matches': matches,
    }
