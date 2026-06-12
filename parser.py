from __future__ import annotations

import gzip
import os
import zlib
from io import BytesIO
from typing import Iterator

from nbt import nbt

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


def find_region_dirs(base: str) -> list[str]:
    """Return every block-region directory in the save.

    A save stores block data in directories literally named ``region``:
    the Overworld at ``<world>/region``, the Nether at ``<world>/DIM-1/region``
    and the End at ``<world>/DIM1/region``. Portal blocks (and any other block)
    can live in any of them, so we scan them all. Sibling ``.mca`` folders such
    as ``entities`` and ``poi`` hold different NBT and are intentionally skipped.
    """
    region_dirs: list[str] = []
    for root, _dirs, files in os.walk(base):
        if os.path.basename(os.path.normpath(root)) != 'region':
            continue
        if any(file_name.endswith('.mca') for file_name in files):
            region_dirs.append(root)
    if not region_dirs:
        raise FileNotFoundError('No region files found. Is this a valid Minecraft save?')
    return region_dirs


def _decompress(compression: int, payload: bytes) -> bytes:
    if compression == _COMPRESSION_ZLIB:
        return zlib.decompress(payload)
    if compression == _COMPRESSION_GZIP:
        return gzip.decompress(payload)
    if compression == _COMPRESSION_NONE:
        return payload
    raise ValueError(f'Unsupported chunk compression id {compression}')


def _iter_region_chunks(path: str) -> Iterator[nbt.NBTFile]:
    """Yield the parsed NBT of every generated chunk in a ``.mca`` file.

    Chunks that are absent, truncated, or fail to decode are skipped rather
    than aborting the whole region, mirroring how a corrupt chunk should not
    sink an otherwise-readable save.
    """
    with open(path, 'rb') as handle:
        data = handle.read()

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
            chunk_x = entry % 32
            chunk_z = entry // 32
            external = os.path.join(os.path.dirname(path), f'c.{chunk_x}.{chunk_z}.mcc')
            try:
                with open(external, 'rb') as ext_handle:
                    payload = ext_handle.read()
            except OSError:
                continue
            compression &= ~_EXTERNAL_FLAG
        else:
            # The stored length counts the compression byte plus the payload.
            payload = data[start + 5:start + 4 + length]

        try:
            raw = _decompress(compression, payload)
            yield nbt.NBTFile(buffer=BytesIO(raw))
        except Exception:
            continue


def normalize_block_type(block_type: str) -> str:
    if ':' not in block_type:
        return 'minecraft:' + block_type
    return block_type


def _decode_section(data: list[int], bits: int):
    """Yield ``(local_index, palette_index)`` for all 4096 blocks in a section.

    Modern (>= 1.16 / 20w17a) packing does not let a palette index straddle a
    64-bit long boundary, so each long holds ``64 // bits`` whole indices.
    """
    per_long = 64 // bits
    mask = (1 << bits) - 1
    idx = 0
    for long_val in data:
        if long_val < 0:
            long_val += 1 << 64
        for _ in range(per_long):
            if idx >= 4096:
                return
            yield idx, long_val & mask
            long_val >>= bits
            idx += 1


def scan_region_file(file_path: str, block_type: str) -> list[dict[str, int]]:
    """Return the world coordinates of every ``block_type`` block in one ``.mca``.

    This is the unit of parallel work: each region file is independent, so the
    server fans these calls out across a process pool. Keeping it a plain
    module-level function (taking only picklable ``str`` arguments and returning
    a plain list of dicts) is what lets it cross the process boundary.
    """
    target_block = normalize_block_type(block_type)
    matches: list[dict[str, int]] = []

    for chunk in _iter_region_chunks(file_path):
        world_chunk_x = chunk['xPos'].value
        world_chunk_z = chunk['zPos'].value

        for section in chunk['sections']:
            block_states = section['block_states']
            palette = block_states['palette']
            names = [block['Name'].value for block in palette]
            if target_block not in names:
                continue

            section_y = section['Y'].value

            # A section with no ``data`` is uniformly palette[0].
            if 'data' not in block_states:
                decoded = ((i, 0) for i in range(4096))
            else:
                bits = max((len(palette) - 1).bit_length(), 4)
                decoded = _decode_section(block_states['data'].value, bits)

            for local_index, palette_index in decoded:
                if names[palette_index] != target_block:
                    continue

                x = local_index % 16
                z = (local_index // 16) % 16
                y = local_index // 256
                matches.append(
                    {
                        'x': world_chunk_x * 16 + x,
                        'y': section_y * 16 + y,
                        'z': world_chunk_z * 16 + z,
                    }
                )

    return matches


def scan_for_block(region_dir: str, block_type: str) -> list[dict[str, int]]:
    """Scan every ``.mca`` in a single region directory (sequential convenience)."""
    matches: list[dict[str, int]] = []
    for file_name in os.listdir(region_dir):
        if file_name.endswith('.mca'):
            matches.extend(scan_region_file(os.path.join(region_dir, file_name), block_type))
    return matches
