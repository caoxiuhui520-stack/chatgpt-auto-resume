"""Minimal, read-only LevelDB reader.

The ChatGPT Desktop app stores the conversation list in Chromium's
``Local Storage``, which is a LevelDB database. ``plyvel`` does not ship a
Windows wheel for the Python we use, so this module reads the format directly
- SSTables (``.ldb``) and the write-ahead log (``.log``) - and never writes a
byte. It exists purely so the ConversationDiscoveryProvider can list local
conversations without any native dependency.

Only the parts of the LevelDB format that matter here are implemented:

* the table footer (metaindex + index handles, 8-byte magic),
* block handles (offset + size varints),
* data-block entries (shared/non_shared/value-length varints),
* the restart array at the end of a block,
* optional Snappy decompression of data blocks,
* the write-ahead log record framing + write-batch payloads.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Iterator

MAGIC = b"\x57\xfb\x80\x8b\x24\x75\x47\xdb"
FOOTER_SIZE = 48
BLOCK_TRAILER = 5  # 1 byte compression type + 4 byte crc
NO_COMPRESSION = 0
SNAPPY_COMPRESSION = 1


class LevelDBError(Exception):
    pass


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Return (value, new_pos) for a base-128 varint at ``pos``."""
    result = 0
    shift = 0
    while pos < len(data):
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, pos
        shift += 7
        if shift > 63:
            raise LevelDBError("varint too long")
    raise LevelDBError("unexpected end of data reading varint")


def _snappy_decompress(data: bytes) -> bytes:
    """Decompress a Snappy-format stream (Chromium uses the raw block format).

    Falls back to ``python-snappy`` when available; otherwise a small built-in
    decoder handles the common cases (literals, copies).
    """
    try:
        import cramjam

        # LevelDB compresses with Snappy's *raw* block format (no length
        # preamble), which is ``decompress_raw`` not ``decompress``.
        return bytes(cramjam.snappy.decompress_raw(data))
    except Exception:  # noqa: BLE001
        pass
    try:
        import snappy  # type: ignore

        return snappy.decompress(data)
    except Exception:  # noqa: BLE001
        pass

    # --- minimal Snappy (raw block) decoder --------------------------------
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        tag = data[i]
        i += 1
        kind = tag & 0x3
        if kind == 0:  # literal
            length = (tag >> 2) + 1
            if length > 60:
                nbytes = length - 60  # 1..4 extra little-endian length bytes
                length = int.from_bytes(data[i:i + nbytes], "little") + 1
                i += nbytes
            out += data[i:i + length]
            i += length
        elif kind in (1, 2, 3):  # copy with 1/2/4 byte offset
            if kind == 1:
                length = ((tag >> 2) & 0x7) + 4
                offset = ((tag >> 5) << 8) | data[i]
                i += 1
            elif kind == 2:
                length = (tag >> 2) + 1
                offset = int.from_bytes(data[i:i + 2], "little")
                i += 2
            else:
                length = (tag >> 2) + 1
                offset = int.from_bytes(data[i:i + 4], "little")
                i += 4
            if offset == 0 or offset > len(out):
                raise LevelDBError("bad snappy copy offset")
            for _ in range(length):
                out.append(out[-offset])
        else:  # pragma: no cover - kind is 2 bits
            raise LevelDBError("bad snappy tag")
    return bytes(out)


def _read_block(data: bytes, offset: int, size: int) -> bytes:
    """Read one block. ``size`` is the content length *excluding* the 5-byte
    trailer (compression type + crc) that follows it."""
    if offset + size + BLOCK_TRAILER > len(data):
        raise LevelDBError("block handle out of range")
    contents = data[offset:offset + size]
    trailer = data[offset + size:offset + size + BLOCK_TRAILER]
    compression = trailer[0]
    if compression == NO_COMPRESSION:
        return contents
    if compression == SNAPPY_COMPRESSION:
        return _snappy_decompress(contents)
    raise LevelDBError(f"unknown compression type {compression}")


def _parse_block(block: bytes) -> list[tuple[bytes, bytes]]:
    """Return [(key, value)] entries from one data/index block."""
    if not block:
        return []
    n = len(block)
    # trailer: num_restarts is the last 4 bytes (little endian).
    if n < 4:
        raise LevelDBError("block too small for restart count")
    num_restarts = struct.unpack("<I", block[-4:])[0]
    restarts_start = n - 4 - 4 * num_restarts
    entries: list[tuple[bytes, bytes]] = []
    pos = 0
    last_key = b""
    while pos < restarts_start:
        shared, pos = _read_varint(block, pos)
        non_shared, pos = _read_varint(block, pos)
        value_len, pos = _read_varint(block, pos)
        key_delta = block[pos:pos + non_shared]
        pos += non_shared
        value = block[pos:pos + value_len]
        pos += value_len
        key = last_key[:shared] + key_delta
        last_key = key
        entries.append((key, value))
    return entries


def _block_handle(encoded: bytes, pos: int) -> tuple[int, int, int]:
    offset, pos = _read_varint(encoded, pos)
    size, pos = _read_varint(encoded, pos)
    return offset, size, pos


class _SSTable:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = path.read_bytes()

    def iter_entries(self) -> Iterator[tuple[bytes, bytes]]:
        data = self.data
        if len(data) < FOOTER_SIZE or data[-8:] != MAGIC:
            raise LevelDBError(f"{self.path.name}: bad footer")
        footer = data[-FOOTER_SIZE:]
        # Footer layout: metaindex handle (offset+size varints) immediately
        # followed by the index handle, then zero padding to 40 bytes, then
        # the 8-byte magic. The two handles are NOT fixed 20-byte slots.
        _meta_offset, _meta_size, pos = _block_handle(footer, 0)
        index_offset, index_size, _ = _block_handle(footer, pos)
        index_block = _read_block(data, index_offset, index_size)
        index_entries = _parse_block(index_block)
        # index entries: key = last key of the block, value = block handle.
        for _last_key, handle_bytes in index_entries:
            boff, bsize, _ = _block_handle(handle_bytes, 0)
            block = _read_block(data, boff, bsize)
            yield from _parse_block(block)


class _WAL:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = path.read_bytes()

    def iter_batches(self) -> Iterator[bytes]:
        """Yield write-batch payloads, reassembled across record fragments."""
        data = self.data
        pos = 0
        batch = bytearray()
        while pos + 7 <= len(data):
            _crc = struct.unpack("<I", data[pos:pos + 4])[0]
            length = struct.unpack("<H", data[pos + 4:pos + 6])[0]
            rectype = data[pos + 6]
            pos += 7
            payload = data[pos:pos + length]
            pos += length
            if rectype == 1:  # FULL
                yield bytes(payload)
                batch = bytearray()
            elif rectype == 2:  # FIRST
                batch = bytearray(payload)
            elif rectype == 3:  # MIDDLE
                batch += payload
            elif rectype == 4:  # LAST
                batch += payload
                yield bytes(batch)
                batch = bytearray()
            else:
                raise LevelDBError(f"unknown WAL record type {rectype}")

    def iter_entries(self) -> Iterator[tuple[bytes, bytes]]:
        for batch in self.iter_batches():
            if len(batch) < 12:
                continue
            # write batch: sequence(8) + count(4) + ops...
            pos = 12
            for _ in range(struct.unpack("<I", batch[8:12])[0]):
                if pos >= len(batch):
                    break
                op_type = batch[pos]
                pos += 1
                key_len, pos = _read_varint(batch, pos)
                key = batch[pos:pos + key_len]
                pos += key_len
                if op_type == 1:  # put
                    value_len, pos = _read_varint(batch, pos)
                    value = batch[pos:pos + value_len]
                    pos += value_len
                    yield key, value
                # op_type == 0 is delete: no value.


class LevelDB:
    """Read-only view over one LevelDB directory.

    ``items()`` yields (key, value) with the latest write winning. SSTables
    are read oldest-to-newest and the WAL last, matching LevelDB semantics.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def items(self) -> dict[bytes, bytes]:
        result: dict[bytes, bytes] = {}

        tables = sorted(self.path.glob("*.ldb"), key=lambda p: int(p.stem))
        for table in tables:
            try:
                for key, value in _SSTable(table).iter_entries():
                    result[key] = value
            except LevelDBError as exc:
                # A corrupt or unexpected table should not kill discovery.
                from app.utils.logging_setup import get_logger

                get_logger("leveldb").warning("skipping %s: %s", table.name, exc)

        logs = sorted(self.path.glob("*.log"), key=lambda p: int(p.stem))
        for log in logs:
            try:
                for key, value in _WAL(log).iter_entries():
                    result[key] = value
            except LevelDBError as exc:
                from app.utils.logging_setup import get_logger

                get_logger("leveldb").warning("skipping %s: %s", log.name, exc)

        return result
