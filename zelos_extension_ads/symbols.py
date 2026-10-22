"""The PLC's symbol and datatype tables, flattened into the scalar fields traced.

Both tables come from the target on every connect (ADS symbol upload 0xF00B and
datatype upload 0xF00E), so a program download shows up after the next
reconnect instead of going stale. Structs and arrays are flattened to their
scalar leaves (`MAIN.stMotor.fSpeed`, `MAIN.arrTemps[2]`); pointers, references,
interfaces and anything without a scalar leaf are skipped.

Trace layout: the first segment of a symbol (its POU or GVL) is the event, the
rest its field, both through the SDK's sanitizer. A symbol map file groups and
names fields explicitly instead.
"""

from __future__ import annotations

import json
import math
import re
import struct
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import zelos_sdk

# ADS datatype ids (ADST_*) of the scalars this extension traces.
ADST_INT16, ADST_INT32, ADST_REAL32, ADST_REAL64 = 2, 3, 4, 5
ADST_INT8, ADST_UINT8, ADST_UINT16, ADST_UINT32, ADST_INT64, ADST_UINT64 = 16, 17, 18, 19, 20, 21
ADST_STRING, ADST_WSTRING, ADST_BIT, ADST_BIGTYPE = 30, 31, 33, 65

#: ADS type id -> (SDK datatype, struct format, size). Strings are variable length.
SCALARS: dict[int, tuple[zelos_sdk.DataType, str, int]] = {
    ADST_BIT: (zelos_sdk.DataType.Boolean, "<?", 1),
    ADST_INT8: (zelos_sdk.DataType.Int8, "<b", 1),
    ADST_UINT8: (zelos_sdk.DataType.UInt8, "<B", 1),
    ADST_INT16: (zelos_sdk.DataType.Int16, "<h", 2),
    ADST_UINT16: (zelos_sdk.DataType.UInt16, "<H", 2),
    ADST_INT32: (zelos_sdk.DataType.Int32, "<i", 4),
    ADST_UINT32: (zelos_sdk.DataType.UInt32, "<I", 4),
    ADST_INT64: (zelos_sdk.DataType.Int64, "<q", 8),
    ADST_UINT64: (zelos_sdk.DataType.UInt64, "<Q", 8),
    ADST_REAL32: (zelos_sdk.DataType.Float32, "<f", 4),
    ADST_REAL64: (zelos_sdk.DataType.Float64, "<d", 8),
    ADST_STRING: (zelos_sdk.DataType.String, "", 0),
    ADST_WSTRING: (zelos_sdk.DataType.String, "", 0),
}
INT_RANGES = {
    f: (-(2 ** (8 * s - 1)), 2 ** (8 * s - 1) - 1) if f.islower() else (0, 2 ** (8 * s) - 1)
    for _, (_, f, s) in SCALARS.items()
    if f and f != "<?" and f[1] not in "fd"
}
BOOL_TRUE = frozenset({"true", "1", "on", "yes"})
BOOL_FALSE = frozenset({"false", "0", "off", "no"})

#: Symbols no one traces by default: TwinCAT's own GVLs and Beckhoff library globals.
#: Globs over the symbol path, like `exclude`; `include` wins over these.
SYSTEM_GLOBALS = (
    "TwinCAT_*.**",  # TwinCAT_SystemInfoVarList, TwinCAT_PreventOnlineChangeGvl
    "Constants.**",
    "Global_Version.**",
    "LREAL_CONST.**",
    "Tc2_*.**",  # Beckhoff library globals (Tc2_MC2, Tc2_System, ...)
    "Tc3_*.**",
)

#: The per-target health event. Sanitized map and symbol names never start
#: with `_`, so neither can take it.
HEALTH_EVENT = "_target"

_SYMBOL_HEADER = struct.Struct("<6I3H")  # entryLength, iGroup, iOffs, size, dataType, flags, 3 lens
_TYPE_HEADER = struct.Struct("<8I5H")  # + version, hashes, offs; lens, arrayDim, subItems
_ARRAY_INFO = struct.Struct("<iI")  # lower bound, element count
_UNRESOLVED = ("POINTER TO ", "REFERENCE TO ")
# ADSDATATYPEFLAG_*: a BITVALUES member's offset and size are in bits; STATIC and
# PROPITEM members hold no data at their offset.
FLAG_BITVALUES, FLAG_PROPITEM, FLAG_STATIC = 0x20, 0x40, 0x20000


@dataclass
class SymbolEntry:
    """One row of the symbol upload."""

    name: str
    type_name: str
    index_group: int
    index_offset: int
    size: int
    data_type: int
    flags: int
    comment: str = ""


@dataclass
class TypeEntry:
    """One row of the datatype upload: a base type, alias, array or struct."""

    name: str
    type_name: str  # element type of an array, base type of an alias, "" for a base
    size: int
    offset: int  # of a member within its struct
    data_type: int
    flags: int
    dims: list[tuple[int, int]] = field(default_factory=list)  # (lower bound, count)
    members: list[TypeEntry] = field(default_factory=list)


def _cstrings(buf: bytes, offset: int, lengths: Sequence[int]) -> tuple[list[str], int]:
    out = []
    for length in lengths:
        out.append(buf[offset : offset + length].decode("latin-1"))
        offset += length + 1  # null terminator
    return out, offset


def parse_symbols(buf: bytes) -> list[SymbolEntry]:
    """The symbol upload (0xF00B) as entries, in table order."""
    entries, offset = [], 0
    while offset + _SYMBOL_HEADER.size <= len(buf):
        length, group, index, size, dtype, flags, nl, tl, cl = _SYMBOL_HEADER.unpack_from(
            buf, offset
        )
        if length < _SYMBOL_HEADER.size:
            raise ValueError(f"corrupt symbol entry at {offset}")
        (name, type_name, comment), _ = _cstrings(buf, offset + _SYMBOL_HEADER.size, (nl, tl, cl))
        entries.append(SymbolEntry(name, type_name, group, index, size, dtype, flags, comment))
        offset += length
    return entries


def _parse_type(buf: bytes, offset: int) -> tuple[TypeEntry, int]:
    fields = _TYPE_HEADER.unpack_from(buf, offset)
    length, _, _, _, size, offs, dtype, flags, nl, tl, cl, dims, subs = fields
    if length < _TYPE_HEADER.size:
        raise ValueError(f"corrupt datatype entry at {offset}")
    (name, type_name, _), p = _cstrings(buf, offset + _TYPE_HEADER.size, (nl, tl, cl))
    entry = TypeEntry(name, type_name, size, offs, dtype, flags)
    for _ in range(dims):
        entry.dims.append(_ARRAY_INFO.unpack_from(buf, p))
        p += _ARRAY_INFO.size
    for _ in range(subs):
        member, p = _parse_type(buf, p)
        entry.members.append(member)
    # Trailing GUID, attributes and enum infos are inside `length`.
    return entry, offset + length


def parse_types(buf: bytes) -> dict[str, TypeEntry]:
    """The datatype upload (0xF00E) by type name."""
    types, offset = {}, 0
    while offset + _TYPE_HEADER.size <= len(buf):
        entry, offset = _parse_type(buf, offset)
        types[entry.name] = entry
    return types


@dataclass
class Field:
    """One scalar PLC variable as traced: its address and its place in the trace."""

    symbol: str  # full PLC path, e.g. MAIN.stMotor.fSpeed
    event: str  # trace event (sanitized)
    name: str  # trace field (sanitized)
    index_group: int
    index_offset: int
    size: int
    data_type: int
    type_name: str
    comment: str = ""
    unit: str = ""
    scale: float = 1.0
    bit: int = -1  # a BIT struct member: its bit in the byte at index_offset
    root: str = ""  # the symbol-table entry holding it, e.g. MAIN.stMotor

    @property
    def datatype(self) -> zelos_sdk.DataType:
        """The trace datatype: the PLC's, or Float64 once a map scale applies (an INT
        scaled by 0.1 is no INT; the SDK truncates, then raises out of range)."""
        if self.scale != 1.0 and self.data_type not in (ADST_BIT, ADST_STRING, ADST_WSTRING):
            return zelos_sdk.DataType.Float64
        return SCALARS[self.data_type][0]

    @property
    def key(self) -> str:
        return f"{self.event}/{self.name}"


_ARRAY_OF = re.compile(r"^ARRAY\s*\[.*\]\s*OF\s+(.+)$", re.IGNORECASE)


def _element_type(type_name: str) -> str:
    """`REAL` of `ARRAY [0..3] OF REAL`; "" for any other type name."""
    m = _ARRAY_OF.match(type_name)
    return m.group(1).strip() if m else ""


def _offsets(
    path: str, type_name: str, size: int, data_type: int, types: dict[str, TypeEntry]
) -> list[tuple[str, int, int, int, str, int]]:
    """Leaves with their byte offset from the symbol's start, and their bit (-1 if none).

    A leaf is a scalar of exactly its type's size: an aggregate the tables left
    unresolved is skipped rather than read, or written zero-padded, as one.
    """
    out = []
    # (path, type name, size, ADS type, offset, depth, a member's own array dims, bit)
    stack: list[tuple[str, str, int, int, int, int, list[tuple[int, int]], int]] = [
        (path, type_name, size, data_type, 0, 0, [], -1)
    ]
    while stack:
        p, tn, sz, dt, base, depth, inline, bit = stack.pop()
        if depth > 16 or tn.startswith(_UNRESOLVED):
            continue
        entry = types.get(tn)
        # A member carries its dims inline; the table may lack its anonymous array type.
        dims = inline or (entry.dims if entry is not None else [])
        if dims:
            if entry is not None and entry.dims:
                element_type, edt = entry.type_name, entry.data_type
            else:
                element_type = _element_type(tn)
                edt = types[element_type].data_type if element_type in types else dt
            count = math.prod(n for _, n in dims)
            if not element_type or count <= 0 or sz % count:
                continue
            element = sz // count
            for flat in reversed(range(count)):
                indices, rest = [], flat
                for _, n in reversed(dims):
                    indices.append(rest % n)
                    rest //= n
                index = ",".join(
                    str(lo + i) for (lo, _), i in zip(dims, reversed(indices), strict=True)
                )
                stack.append(
                    (
                        f"{p}[{index}]",
                        element_type,
                        element,
                        edt,
                        base + flat * element,
                        depth + 1,
                        [],
                        -1,
                    )
                )
        elif entry is not None and entry.members:
            for member in reversed(entry.members):
                if member.flags & (FLAG_STATIC | FLAG_PROPITEM):
                    continue
                offset, member_bit = member.offset, -1
                if member.flags & FLAG_BITVALUES:
                    # Offset and size in bits; only a single BIT is a leaf.
                    if member.data_type != ADST_BIT or member.size != 1 or member.dims:
                        continue
                    offset, member_bit = member.offset // 8, member.offset % 8
                stack.append(
                    (
                        f"{p}.{member.name}",
                        member.type_name,
                        member.size,
                        member.data_type,
                        base + offset,
                        depth + 1,
                        member.dims,
                        member_bit,
                    )
                )
        elif dt in SCALARS:
            fixed = SCALARS[dt][2]
            if fixed and sz != fixed:
                continue
            out.append((p, base, sz, dt, tn, bit))
        elif entry is not None and entry.type_name and entry.type_name != tn:
            stack.append((p, entry.type_name, sz, entry.data_type, base, depth + 1, [], bit))
    return out


def sanitize(name: str, kind: str) -> str:
    """A name addressable in the trace catalog; the SDK owns the grammar."""
    return zelos_sdk.sanitize_name(name, kind=kind)


def trace_names(symbol: str) -> tuple[str, str]:
    """(event, field) of a symbol: its POU or GVL, and the rest with `.`, `[`, `]` and `,`
    folded to `_` (`MAIN.arrTemps[2]` -> `MAIN`, `arrTemps_2`)."""
    pou, _, rest = symbol.partition(".")
    return sanitize(pou, "event"), sanitize(re.sub(r"[.\[\],]+", "_", rest).strip("_"), "field")


def flatten(symbols: Iterable[SymbolEntry], types: dict[str, TypeEntry]) -> list[Field]:
    """Every scalar leaf of every symbol, addressed and named for the trace."""
    fields = []
    for symbol in symbols:
        if "." not in symbol.name:
            continue  # not a PLC variable (no POU or GVL)
        for path, offset, size, dtype, type_name, bit in _offsets(
            symbol.name, symbol.type_name, symbol.size, symbol.data_type, types
        ):
            event, name = trace_names(path)
            fields.append(
                Field(
                    path,
                    event,
                    name,
                    symbol.index_group,
                    symbol.index_offset + offset,
                    size,
                    dtype,
                    type_name,
                    symbol.comment.strip(),
                    bit=bit,
                    root=symbol.name,
                )
            )
    return fields


# ─── Values ─────────────────────────────────────────────────────────────────


def decode(data: bytes, data_type: int, scale: float = 1.0, bit: int = -1) -> Any:
    """A Python value from a symbol's bytes; None for a malformed buffer. `bit` picks
    one bit of the first byte (a BIT struct member)."""
    if bit >= 0:
        return bool(data[0] >> bit & 1) if data else None
    if data_type == ADST_STRING:
        # TwinCAT STRING is Windows-1252, as pyads and ads-client decode it.
        return data.split(b"\0", 1)[0].decode("cp1252", errors="replace")
    if data_type == ADST_WSTRING:
        return data.decode("utf-16-le", errors="replace").split("\0", 1)[0]
    _, fmt, size = SCALARS[data_type]
    if len(data) < size:
        return None
    value = struct.unpack_from(fmt, data)[0]
    if scale != 1.0 and data_type != ADST_BIT:
        return value * scale
    return value


@dataclass(eq=False)
class Block:
    """The traced leaves of one symbol-table entry, read as one span of bytes."""

    index_group: int
    start: int
    size: int
    fields: list[Field]

    def values(self, data: bytes) -> Iterator[tuple[Field, Any]]:
        """Each leaf decoded from the span's bytes; None for one `data` does not cover."""
        for f in self.fields:
            at = f.index_offset - self.start
            if len(data) < at + f.size:
                yield f, None
            else:
                yield f, decode(data[at : at + f.size], f.data_type, f.scale, f.bit)


def blocks(fields: Iterable[Field], max_bytes: int) -> tuple[list[Block], list[Field]]:
    """Fields grouped by symbol-table entry and index group, each spanning its traced
    leaves, in first-seen order; the leaves of a span over `max_bytes` come back apart."""
    grouped: dict[tuple[str, int], list[Field]] = {}
    for f in fields:
        grouped.setdefault((f.root or f.symbol, f.index_group), []).append(f)
    out, rest = [], []
    for fs in grouped.values():
        start = min(f.index_offset for f in fs)
        size = max(f.index_offset + f.size for f in fs) - start
        if size > max_bytes:
            rest.extend(fs)
        else:
            out.append(Block(fs[0].index_group, start, size, fs))
    return out, rest


def encode(value: Any, data_type: int, size: int) -> bytes:
    """A symbol's bytes from a Python value, `size` bytes exactly."""
    if data_type == ADST_STRING:
        raw = str(value).encode("cp1252", errors="replace")[: max(size - 1, 0)]
        return raw.ljust(size, b"\0")
    if data_type == ADST_WSTRING:
        raw = str(value).encode("utf-16-le")[: max(size - 2, 0) & ~1]
        return raw.ljust(size, b"\0")
    _, fmt, _ = SCALARS[data_type]
    data = struct.pack(fmt, value)
    if len(data) != size:  # never pad: the bytes past the value belong to something else
        raise ValueError(f"symbol holds {size} bytes, its type {len(data)}")
    return data


def coerce_text(text: str, data_type: int) -> Any:
    """Text from an action parameter as a value of `data_type`; ValueError if not."""
    text = text.strip()
    if data_type == ADST_BIT:
        if text.lower() in BOOL_TRUE:
            return True
        if text.lower() in BOOL_FALSE:
            return False
        raise ValueError(f"'{text}' is not a boolean (true/false)")
    if data_type in (ADST_STRING, ADST_WSTRING):
        return text
    _, fmt, _ = SCALARS[data_type]
    if fmt[1] in "fd":
        try:
            return float(text)
        except ValueError as e:
            raise ValueError(f"'{text}' is not a number") from e
    try:
        value = int(text, 0) if not text.lstrip("-").isdigit() else int(text)
    except ValueError as e:
        raise ValueError(f"'{text}' is not an integer") from e
    low, high = INT_RANGES[fmt]
    if not low <= value <= high:
        raise ValueError(f"{value} is outside {low}..{high}")
    return value


# ─── Filters and the map ────────────────────────────────────────────────────


def _match(pattern: str, path: str) -> bool:
    """Glob over a dotted symbol path: `*` within one segment, `**` any number."""
    return _match_segments(tuple(pattern.split(".")), tuple(path.split(".")))


def _match_segments(pattern: tuple[str, ...], path: tuple[str, ...]) -> bool:
    if not pattern:
        return not path
    head, rest = pattern[0], pattern[1:]
    if head == "**":
        return any(_match_segments(rest, path[i:]) for i in range(len(path) + 1))
    return bool(path) and fnmatchcase(path[0], head) and _match_segments(rest, path[1:])


@dataclass
class SymbolFilter:
    """Include and exclude globs over the symbol path (`MAIN.*`, `GVL_IO.**`, `**.bDebug`).

    Empty include traces everything not excluded. Exclude wins over include;
    SYSTEM_GLOBALS are excluded unless an include names them.
    """

    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

    @classmethod
    def parse(cls, include: Sequence[str] = (), exclude: Sequence[str] = ()) -> SymbolFilter:
        """Raises ValueError on an empty pattern or segment."""
        for raw in (*include, *exclude):
            if not raw.strip() or not all(raw.strip().split(".")):
                raise ValueError(f"invalid symbol pattern '{raw}'")
        return cls(tuple(p.strip() for p in include), tuple(p.strip() for p in exclude))

    def keeps(self, symbol: str) -> bool:
        if any(_match(p, symbol) for p in self.exclude):
            return False
        if any(_match(p, symbol) for p in self.include):
            return True
        if self.include:
            return False
        return not any(_match(p, symbol) for p in SYSTEM_GLOBALS)


@dataclass
class MapEntry:
    """One symbol of a map file: which PLC variable, and what to call it."""

    symbol: str
    name: str = ""
    unit: str = ""
    scale: float = 1.0


@dataclass
class SymbolMap:
    """Events and their symbols, from a JSON file:

    {"events": {"motor": [{"symbol": "MAIN.stMotor.fSpeed", "name": "speed", "unit": "rpm"}]}}

    Event and field names are sanitized at load; duplicates after sanitization
    are a load error (a silent clobber would drop data). `name` defaults to the
    symbol's own trace field name.
    """

    events: dict[str, list[MapEntry]] = field(default_factory=dict)

    @classmethod
    def from_file(cls, path: str | Path) -> SymbolMap:
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Symbol map file not found: {path}")
        return cls.from_dict(json.loads(path.read_text()))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SymbolMap:
        events: dict[str, list[MapEntry]] = {}
        for raw_event, raw_entries in (data.get("events") or {}).items():
            event = sanitize(str(raw_event), "event")
            if not event or event.startswith("_"):
                raise ValueError(f"Invalid event name '{raw_event}'")
            if event in events:
                raise ValueError(f"Duplicate event name '{event}'")
            if not isinstance(raw_entries, list):
                raise ValueError(f"Event '{raw_event}' must be a list of symbols")
            entries, seen = [], set()
            for raw in raw_entries:
                symbol = str(raw.get("symbol", "")).strip()
                if not symbol:
                    raise ValueError(f"Event '{raw_event}': a symbol has no 'symbol'")
                name = sanitize(str(raw.get("name") or trace_names(symbol)[1]), "field")
                if not name or name in seen:
                    raise ValueError(f"Event '{raw_event}': duplicate or empty field name '{name}'")
                seen.add(name)
                entries.append(
                    MapEntry(symbol, name, str(raw.get("unit", "")), float(raw.get("scale", 1.0)))
                )
            events[event] = entries
        return cls(events)

    @property
    def symbols(self) -> list[str]:
        return [e.symbol for entries in self.events.values() for e in entries]

    def apply(self, fields: list[Field]) -> tuple[list[Field], list[str]]:
        """The map's fields, addressed from the uploaded `fields`, and the symbols missing."""
        by_symbol = {f.symbol.lower(): f for f in fields}
        out, missing = [], []
        for event, entries in self.events.items():
            for entry in entries:
                found = by_symbol.get(entry.symbol.lower())
                if found is None:
                    missing.append(entry.symbol)
                    continue
                out.append(
                    Field(
                        found.symbol,
                        event,
                        entry.name,
                        found.index_group,
                        found.index_offset,
                        found.size,
                        found.data_type,
                        found.type_name,
                        found.comment,
                        entry.unit,
                        entry.scale,
                        found.bit,
                        found.root,
                    )
                )
        return out, missing


def layout(fields: list[Field]) -> tuple[dict[str, list[Field]], list[str]]:
    """Fields by event, in symbol order; a name taken twice within an event is
    dropped (and listed), never silently re-pointed."""
    events: dict[str, list[Field]] = {}
    taken: set[str] = set()
    dropped = []
    for f in fields:
        if not f.event or not f.name:
            dropped.append(f.symbol)
            continue
        if f.key in taken:
            dropped.append(f.symbol)
            continue
        taken.add(f.key)
        events.setdefault(f.event, []).append(f)
    return events, dropped
