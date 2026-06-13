from __future__ import annotations

import gzip
import os
import zlib
from typing import Callable, Iterator, Mapping

import fastnbt

# Region files (.mca) layout: a 4 KiB location table (1024 chunks, 4 bytes
# each) followed by a 4 KiB timestamp table, then chunk payloads aligned to
# 4 KiB "sectors". Each location entry is a 3-byte big-endian sector offset
# plus a 1-byte sector count; (0, 0) means the chunk has not been generated.
_SECTOR_BYTES = 4096
_LOCATION_TABLE_BYTES = _SECTOR_BYTES

# Compression ids stored in the 5th byte of a chunk payload. The high bit
# (0x80) flags an "oversized" chunk whose payload lives in an external
# c.<x>.<z>.mcc file; the low bits then give the real compression id.
_COMPRESSION_GZIP = 1
_COMPRESSION_ZLIB = 2
_COMPRESSION_NONE = 3
_EXTERNAL_FLAG = 0x80


def _decompress(compression: int, payload: bytes) -> bytes:
    if compression == _COMPRESSION_ZLIB:
        return zlib.decompress(payload)
    if compression == _COMPRESSION_GZIP:
        return gzip.decompress(payload)
    if compression == _COMPRESSION_NONE:
        return payload
    raise ValueError(f'Unsupported chunk compression id {compression}')


def _walk_region(data: bytes, read_external: Callable[[int, int], bytes | None]) -> Iterator[bytes]:
    """Yield the decompressed NBT bytes of every generated chunk in a region file.

    ``data`` is the raw ``.mca`` content. ``read_external(chunk_x, chunk_z)``
    supplies the payload of an oversized chunk's sibling ``c.<x>.<z>.mcc`` file
    (returning ``None`` when it can't be found); this is the one piece that
    differs between reading from disk and reading straight from the upload zip.

    Chunks that are absent, truncated, or fail to decompress are skipped rather
    than aborting the whole region, mirroring how a corrupt chunk should not
    sink an otherwise-readable save.
    """
    # A valid region file always carries the full 4 KiB location table; a
    # shorter file (e.g. a 0-byte placeholder) holds no chunks.
    if len(data) < _LOCATION_TABLE_BYTES:
        return

    for entry in range(1024):
        loc = entry * 4
        sector_offset = int.from_bytes(data[loc:loc + 3], byteorder='big')
        if sector_offset == 0:
            continue

        start = sector_offset * _SECTOR_BYTES
        if start + 5 > len(data):
            continue

        length = int.from_bytes(data[start:start + 4], byteorder='big')
        if length < 1:
            continue
        compression = data[start + 4]

        if compression & _EXTERNAL_FLAG:
            # Oversized chunk: payload lives in a sibling c.<x>.<z>.mcc file.
            payload = read_external(entry % 32, entry // 32)
            if payload is None:
                continue
            compression &= ~_EXTERNAL_FLAG
        else:
            # The stored length counts the compression byte plus the payload.
            payload = data[start + 5:start + 4 + length]

        try:
            yield _decompress(compression, payload)
        except Exception:
            continue


def _iter_region_chunks(path: str) -> Iterator[bytes]:
    """Yield decompressed chunk bytes from a ``.mca`` on disk (``.mcc`` siblings alongside)."""
    with open(path, 'rb') as handle:
        data = handle.read()

    def read_external(chunk_x: int, chunk_z: int) -> bytes | None:
        external = os.path.join(os.path.dirname(path), f'c.{chunk_x}.{chunk_z}.mcc')
        try:
            with open(external, 'rb') as ext_handle:
                return ext_handle.read()
        except OSError:
            return None

    yield from _walk_region(data, read_external)


def normalize_block_type(block_type: str) -> str:
    if ':' not in block_type:
        return 'minecraft:' + block_type
    return block_type


def scan_region_bytes(
    mca_bytes: bytes,
    mcc_map: Mapping[str, bytes],
    block_type: str,
) -> list[dict[str, int]]:
    """Return the world coordinates of every ``block_type`` block in one region file.

    This is the unit of parallel work: each region file is independent, so the
    server reads its bytes from the upload zip and fans these calls across a
    process pool — no temp files. ``mca_bytes`` is the raw ``.mca`` content and
    ``mcc_map`` maps ``c.<x>.<z>.mcc`` sidecar names to their bytes (usually
    empty; oversized chunks are rare). All arguments are picklable so the call
    crosses the process boundary cleanly.
    """
    target_block = normalize_block_type(block_type)
    matches: list[dict[str, int]] = []

    def read_external(chunk_x: int, chunk_z: int) -> bytes | None:
        return mcc_map.get(f'c.{chunk_x}.{chunk_z}.mcc')

    for chunk in _walk_region(mca_bytes, read_external):
        fastnbt.scan_chunk(chunk, target_block, matches)
    return matches


def scan_region_file(file_path: str, block_type: str) -> list[dict[str, int]]:
    """Scan one ``.mca`` on disk (kept for benchmarks and direct-from-disk callers)."""
    target_block = normalize_block_type(block_type)
    matches: list[dict[str, int]] = []
    for chunk in _iter_region_chunks(file_path):
        fastnbt.scan_chunk(chunk, target_block, matches)
    return matches
