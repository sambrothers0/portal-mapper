from __future__ import annotations

import asyncio
import os
import tempfile
import zipfile
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager
from pathlib import PurePosixPath

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from parser import normalize_block_type, scan_region_file

DEFAULT_BLOCK_TYPE = 'minecraft:nether_portal'

# Hard ceiling on the upload. The frontend enforces the same limit for UX; this
# is the server-side safety net. The ``content-length`` of a multipart body is
# a little larger than the file itself (boundaries + headers), so we allow a
# small margin over the advertised 4 GiB so a genuine ~4 GiB save isn't bounced.
MAX_UPLOAD_BYTES = 4 * 1024 ** 3
_UPLOAD_LIMIT_BYTES = MAX_UPLOAD_BYTES + 32 * 1024 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One pool reused for the life of the process. Region-file parsing is
    # CPU-bound pure Python, so we spread it across every core; a process pool
    # (not threads) is what actually sidesteps the GIL.
    executor = ProcessPoolExecutor(max_workers=os.cpu_count() or 1)
    app.state.executor = executor
    try:
        yield
    finally:
        executor.shutdown(cancel_futures=True)


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_methods=['*'],
    allow_headers=['*'],
)


def _is_region_member(name: str) -> bool:
    """True for ``.mca``/``.mcc`` files that live directly in a ``region`` dir.

    A save keeps block data in directories literally named ``region`` (the
    Overworld at ``<world>/region``, the Nether at ``<world>/DIM-1/region``,
    the End at ``<world>/DIM1/region``). Everything else in the archive
    (``entities``, ``poi``, ``playerdata``, ``level.dat``, ...) is irrelevant to
    a block search and can be gigabytes, so we never extract it.
    """
    if not (name.endswith('.mca') or name.endswith('.mcc')):
        return False
    parts = PurePosixPath(name).parts
    return len(parts) >= 2 and parts[-2] == 'region'


def _extract_region_files(spooled_file, extract_root: str) -> list[str]:
    """Selectively extract region files from the upload and return ``.mca`` paths.

    Reads straight from Starlette's already-on-disk spooled upload, so the 4 GiB
    archive is never held in memory. ``.mcc`` (oversized-chunk) sidecars are
    extracted too so the parser can find them, but only ``.mca`` paths are
    returned as units of work.
    """
    spooled_file.seek(0)
    mca_paths: list[str] = []
    with zipfile.ZipFile(spooled_file) as archive:
        members = [info for info in archive.infolist() if _is_region_member(info.filename)]
        if not members:
            raise FileNotFoundError('No region files found. Is this a valid Minecraft save?')
        for member in members:
            # ZipFile.extract sanitizes ".." / absolute paths, so this stays
            # inside extract_root.
            out_path = archive.extract(member, extract_root)
            if out_path.endswith('.mca'):
                mca_paths.append(out_path)
    return mca_paths


@app.post('/parse-blocks')
async def parse_blocks(
    request: Request,
    file: UploadFile = File(...),
    block_type: str = Form(default=DEFAULT_BLOCK_TYPE),
):
    if not file.filename or not file.filename.endswith('.zip'):
        raise HTTPException(status_code=400, detail='Upload must be a .zip of the save folder')

    content_length = request.headers.get('content-length')
    if content_length and content_length.isdigit() and int(content_length) > _UPLOAD_LIMIT_BYTES:
        raise HTTPException(status_code=413, detail='Upload exceeds the 4 GB limit')

    loop = asyncio.get_running_loop()
    executor: ProcessPoolExecutor = request.app.state.executor

    with tempfile.TemporaryDirectory() as tmpdir:
        # Extraction is blocking disk/zlib work; run it off the event loop.
        try:
            mca_paths = await loop.run_in_executor(
                None, _extract_region_files, file.file, tmpdir
            )
        except zipfile.BadZipFile as exc:
            raise HTTPException(status_code=400, detail='File is not a valid zip archive') from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        # Fan every region file out across the pool and await them together.
        scans = [
            loop.run_in_executor(executor, scan_region_file, path, block_type)
            for path in mca_paths
        ]
        per_file = await asyncio.gather(*scans)

    matches = [match for file_matches in per_file for match in file_matches]

    return {
        'block_type': normalize_block_type(block_type),
        'count': len(matches),
        'matches': matches,
    }
