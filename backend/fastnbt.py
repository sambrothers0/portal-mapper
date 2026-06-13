from __future__ import annotations

import struct

# A purpose-built, allocation-light NBT reader for the one question this app
# asks: "where are the blocks of type X in this chunk?". A general NBT library
# eagerly materializes a Python object for *every* tag in *every* chunk — block
# entities, heightmaps, biomes, and the packed block ``data`` of every section,
# almost all of which we throw away. Instead this walks the byte stream and
# skips whole subtrees it doesn't need, reads each section's small palette
# first, and only decodes a section's expensive packed ``data`` array when the
# target block actually appears in that section's palette.
#
# Java-edition NBT is big-endian. Tag ids:
_TAG_END = 0
_TAG_BYTE = 1
_TAG_SHORT = 2
_TAG_INT = 3
_TAG_LONG = 4
_TAG_FLOAT = 5
_TAG_DOUBLE = 6
_TAG_BYTE_ARRAY = 7
_TAG_STRING = 8
_TAG_LIST = 9
_TAG_COMPOUND = 10
_TAG_INT_ARRAY = 11
_TAG_LONG_ARRAY = 12

_FIXED_SIZES = {
    _TAG_BYTE: 1,
    _TAG_SHORT: 2,
    _TAG_INT: 4,
    _TAG_LONG: 8,
    _TAG_FLOAT: 4,
    _TAG_DOUBLE: 8,
}

# Pre-built struct readers (compiled once) for the hot primitives.
_u16 = struct.Struct('>H')
_i32 = struct.Struct('>i')


class MatchSink:
    """Counts matches while retaining at most ``limit`` coordinates.

    A target block like ``stone`` can occur tens of millions of times in one
    save. Materializing a dict per occurrence would blow up memory long before
    any response cap applied, so the sink keeps the *true* ``count`` but stops
    storing coordinates once ``limit`` is reached. The caller reports ``count``
    and flags truncation when it exceeds the cap.
    """

    __slots__ = ('limit', 'count', 'matches')

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.count = 0
        self.matches: list[dict[str, int]] = []

    def add(self, x: int, y: int, z: int) -> None:
        self.count += 1
        if len(self.matches) < self.limit:
            self.matches.append({'x': x, 'y': y, 'z': z})


def _skip(buf: bytes, pos: int, tag: int) -> int:
    """Advance ``pos`` past one tag *payload* (no leading id/name) of ``tag``."""
    size = _FIXED_SIZES.get(tag)
    if size is not None:
        return pos + size
    if tag == _TAG_STRING:
        return pos + 2 + _u16.unpack_from(buf, pos)[0]
    if tag == _TAG_BYTE_ARRAY:
        return pos + 4 + _i32.unpack_from(buf, pos)[0]
    if tag == _TAG_INT_ARRAY:
        return pos + 4 + _i32.unpack_from(buf, pos)[0] * 4
    if tag == _TAG_LONG_ARRAY:
        return pos + 4 + _i32.unpack_from(buf, pos)[0] * 8
    if tag == _TAG_LIST:
        elem = buf[pos]
        count = _i32.unpack_from(buf, pos + 1)[0]
        pos += 5
        fixed = _FIXED_SIZES.get(elem)
        if fixed is not None:
            return pos + fixed * count
        for _ in range(count):
            pos = _skip(buf, pos, elem)
        return pos
    if tag == _TAG_COMPOUND:
        while True:
            child = buf[pos]
            pos += 1
            if child == _TAG_END:
                return pos
            pos += 2 + _u16.unpack_from(buf, pos)[0]  # skip the name
            pos = _skip(buf, pos, child)
    raise ValueError(f'Unknown NBT tag id {tag}')


def _read_palette_names(buf: bytes, pos: int) -> list[str]:
    """Read the block ``Name`` of every entry in a ``palette`` list."""
    # List header: element type (compound) + entry count.
    count = _i32.unpack_from(buf, pos + 1)[0]
    pos += 5
    names: list[str] = []
    for _ in range(count):
        name = ''
        while True:
            child = buf[pos]
            pos += 1
            if child == _TAG_END:
                break
            key_len = _u16.unpack_from(buf, pos)[0]
            pos += 2
            key = buf[pos:pos + key_len]
            pos += key_len
            if child == _TAG_STRING and key == b'Name':
                val_len = _u16.unpack_from(buf, pos)[0]
                pos += 2
                name = buf[pos:pos + val_len].decode('utf-8')
                pos += val_len
            else:
                pos = _skip(buf, pos, child)
        names.append(name)
    return names


def _emit_block_states(
    buf: bytes,
    pos: int,
    target: str,
    base_x: int,
    base_y: int,
    base_z: int,
    out: MatchSink,
) -> None:
    """Scan one section's ``block_states`` compound for the target block."""
    palette_pos = -1
    data_pos = -1
    while True:
        child = buf[pos]
        pos += 1
        if child == _TAG_END:
            break
        key_len = _u16.unpack_from(buf, pos)[0]
        pos += 2
        key = buf[pos:pos + key_len]
        pos += key_len
        if child == _TAG_LIST and key == b'palette':
            palette_pos = pos
        elif child == _TAG_LONG_ARRAY and key == b'data':
            data_pos = pos
        pos = _skip(buf, pos, child)

    if palette_pos < 0:
        return
    names = _read_palette_names(buf, palette_pos)
    if target not in names:
        return  # The expensive decode below only happens for matching sections.

    target_indices = {i for i, name in enumerate(names) if name == target}

    # A section with no packed ``data`` is uniformly palette[0].
    if data_pos < 0:
        if 0 in target_indices:
            for idx in range(4096):
                out.add(base_x + (idx & 15), base_y + (idx >> 8), base_z + ((idx >> 4) & 15))
        return

    long_count = _i32.unpack_from(buf, data_pos)[0]
    # A section packs exactly 4096 indices, so the long array is small and
    # fixed-ish (<=1024 longs). Reject absurd counts from crafted input before
    # building a huge struct format / read past the buffer.
    if long_count < 0 or long_count > 4096:
        return
    longs = struct.unpack_from(f'>{long_count}q', buf, data_pos + 4)

    # Modern (>= 1.16) packing keeps every palette index whole within a 64-bit
    # long, so each long holds ``64 // bits`` indices.
    bits = max((len(names) - 1).bit_length(), 4)
    per_long = 64 // bits
    mask = (1 << bits) - 1
    idx = 0
    for value in longs:
        if value < 0:
            value += 1 << 64
        for _ in range(per_long):
            if idx >= 4096:
                break
            if (value & mask) in target_indices:
                out.add(base_x + (idx & 15), base_y + (idx >> 8), base_z + ((idx >> 4) & 15))
            value >>= bits
            idx += 1
        if idx >= 4096:
            break


def _scan_sections(buf: bytes, pos: int, target: str, cx: int, cz: int, out: MatchSink) -> None:
    """Walk the ``sections`` list, scanning each section's block states."""
    elem = buf[pos]
    count = _i32.unpack_from(buf, pos + 1)[0]
    pos += 5
    if elem != _TAG_COMPOUND:
        return

    base_x = cx * 16
    base_z = cz * 16
    for _ in range(count):
        section_y = None
        block_states_pos = -1
        while True:
            child = buf[pos]
            pos += 1
            if child == _TAG_END:
                break
            key_len = _u16.unpack_from(buf, pos)[0]
            pos += 2
            key = buf[pos:pos + key_len]
            pos += key_len
            if child == _TAG_BYTE and key == b'Y':
                section_y = buf[pos]
                if section_y >= 128:
                    section_y -= 256  # Y is a signed byte.
            elif child == _TAG_COMPOUND and key == b'block_states':
                block_states_pos = pos
            pos = _skip(buf, pos, child)

        # Sections that are pure air carry only biomes/lighting (no block_states).
        if block_states_pos >= 0 and section_y is not None:
            _emit_block_states(buf, block_states_pos, target, base_x, section_y * 16, base_z, out)


def scan_chunk(buf: bytes, target: str, out: MatchSink) -> None:
    """Feed every ``target``-block coordinate found in one decompressed chunk to ``out``.

    Silently ignores chunks that are still generating (no ``sections``) or lack
    their position tags, mirroring the "a bad chunk shouldn't sink the save"
    tolerance of the region reader.
    """
    # Root is a named TAG_Compound: id byte, name length, name, then payload.
    if not buf or buf[0] != _TAG_COMPOUND:
        return
    pos = 1 + 2 + _u16.unpack_from(buf, 1)[0]

    chunk_x = None
    chunk_z = None
    sections_pos = -1
    # sections may appear before xPos/zPos in tag order, so locate all three
    # first, then scan once both coordinates are known.
    while True:
        child = buf[pos]
        pos += 1
        if child == _TAG_END:
            break
        key_len = _u16.unpack_from(buf, pos)[0]
        pos += 2
        key = buf[pos:pos + key_len]
        pos += key_len
        if child == _TAG_INT and key == b'xPos':
            chunk_x = _i32.unpack_from(buf, pos)[0]
        elif child == _TAG_INT and key == b'zPos':
            chunk_z = _i32.unpack_from(buf, pos)[0]
        elif child == _TAG_LIST and key == b'sections':
            sections_pos = pos
        pos = _skip(buf, pos, child)

    if chunk_x is None or chunk_z is None or sections_pos < 0:
        return
    _scan_sections(buf, sections_pos, target, chunk_x, chunk_z, out)
