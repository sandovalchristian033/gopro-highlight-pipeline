"""Parser for GoPro's GPMF (General Purpose Metadata Format) KLV streams.

GPMF is a nested key-length-value format. Every node looks like:

    bytes 0-3   FourCC key         e.g. b"ACCL"
    byte  4     type character     e.g. "s" (int16), 0x00 means "nested"
    byte  5     item size          bytes per sample (3 int16s -> 6)
    bytes 6-7   repeat count       number of samples (big endian uint16)
    bytes 8..   payload            item_size * repeat bytes, padded to 4

Everything is big endian. The only structure we care about is:

    DEVC (nested)
      STRM (nested)
        SCAL  scale divisor(s) for the data key in this stream
        ACCL / GYRO / GPS5 / GPS9 / ...   the actual samples

This module has no third-party dependencies on purpose: it has to keep
working years from now without a package ecosystem breaking underneath it.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

# GPMF type character -> (struct format character, bytes per item)
_TYPES: dict[str, tuple[str, int]] = {
    "b": ("b", 1),  # int8
    "B": ("B", 1),  # uint8
    "d": ("d", 8),  # float64
    "f": ("f", 4),  # float32
    "j": ("q", 8),  # int64
    "J": ("Q", 8),  # uint64
    "l": ("i", 4),  # int32
    "L": ("I", 4),  # uint32
    "q": ("i", 4),  # Q15.16 fixed point
    "Q": ("q", 8),  # Q31.32 fixed point
    "s": ("h", 2),  # int16
    "S": ("H", 2),  # uint16
}

# Types whose payload is text rather than numbers.
_STRING_TYPES = frozenset("cFUG")

# Fixed point types carry an implicit divisor.
_FIXED_POINT = {"q": 1 << 16, "Q": 1 << 32}

NESTED = "\x00"


@dataclass
class Node:
    """One GPMF KLV node."""

    key: str
    type: str
    item_size: int
    repeat: int
    raw: bytes
    children: list["Node"] = field(default_factory=list)

    def find(self, key: str) -> "Node | None":
        """First direct child with this FourCC key."""
        for child in self.children:
            if child.key == key:
                return child
        return None

    def find_all(self, key: str) -> list["Node"]:
        return [child for child in self.children if child.key == key]

    def values(self) -> list:
        """Decode the payload.

        Returns a flat list when each sample is a single item (e.g. SCAL with
        one divisor), or a list of lists when each sample is a vector (e.g.
        ACCL, where every sample is 3 axes).
        """
        if self.type in _STRING_TYPES:
            text = self.raw.decode("latin-1", "replace").rstrip("\x00").strip()
            return [text] if text else []

        spec = _TYPES.get(self.type)
        if spec is None or self.item_size == 0:
            return []

        fmt_char, item_bytes = spec
        per_sample = self.item_size // item_bytes
        if per_sample == 0:
            return []

        total = per_sample * self.repeat
        if len(self.raw) < total * item_bytes:
            # Truncated payload (corrupt tail on an interrupted recording).
            total = len(self.raw) // item_bytes
            self.repeat = total // per_sample
            total = per_sample * self.repeat
        if total == 0:
            return []

        flat = struct.unpack_from(">" + fmt_char * total, self.raw)

        divisor = _FIXED_POINT.get(self.type)
        if divisor is not None:
            flat = tuple(value / divisor for value in flat)

        if per_sample == 1:
            return list(flat)
        return [list(flat[i * per_sample : (i + 1) * per_sample]) for i in range(self.repeat)]


def parse(buf: bytes, start: int = 0, end: int | None = None) -> list[Node]:
    """Parse a GPMF buffer into a list of top-level nodes."""
    if end is None:
        end = len(buf)

    nodes: list[Node] = []
    pos = start

    while pos + 8 <= end:
        key = buf[pos : pos + 4].decode("latin-1")
        type_char = chr(buf[pos + 4])
        item_size = buf[pos + 5]
        repeat = struct.unpack_from(">H", buf, pos + 6)[0]
        pos += 8

        payload_len = item_size * repeat
        if pos + payload_len > end:
            break  # truncated node, stop cleanly

        node = Node(key, type_char, item_size, repeat, bytes(buf[pos : pos + payload_len]))
        if type_char == NESTED:
            node.children = parse(buf, pos, pos + payload_len)
        nodes.append(node)

        # Payloads are padded out to a 4 byte boundary.
        pos += (payload_len + 3) & ~3

    return nodes


def scale_values(samples: list, scal: list[float]) -> list:
    """Apply a SCAL divisor to decoded samples.

    SCAL carries either one divisor for every axis, or one divisor per axis
    (GPS5 uses five: lat, lon, altitude, 2D speed, 3D speed).
    """
    if not scal:
        return samples

    if samples and isinstance(samples[0], list):
        width = len(samples[0])
        divisors = scal if len(scal) == width else [scal[0]] * width
        return [
            [value / divisor if divisor else value for value, divisor in zip(sample, divisors)]
            for sample in samples
        ]

    divisor = scal[0]
    if not divisor:
        return samples
    return [value / divisor for value in samples]
