"""A simulated TwinCAT PLC: a symbol table, a datatype table, process memory, a
10 ms task and ADS notifications, without the wire.

`DemoLink` is the in-process `Link` demo mode uses on any platform;
`demo.server` puts the same PLC behind AMS/TCP for the tests and `demo-server`.
"""

from __future__ import annotations

import math
import struct
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from pyads.pyads_ex import ADSError

from zelos_extension_ads import ethercat as ecat
from zelos_extension_ads import symbols as sym
from zelos_extension_ads.symbols import SymbolEntry, TypeEntry
from zelos_extension_ads.transport import (
    ADSIGRP_SYM_RELEASEHND,
    ADSIGRP_SYM_VALBYHND,
    ADSIGRP_SYM_VERSION,
    NotifyCallback,
)

DEMO_NET_ID = "192.168.100.10.1.1"
DEMO_ECAT_NET_ID = "192.168.100.10.2.1"
DEMO_HOST = "127.0.0.1"
DEMO_PORT = 851
TASK_SECONDS = 0.01

IG_DATA = 0x4040
IG_OUTPUTS = 0xF030
ADSSTATE_RUN = 5

# (name, type name, ADS type, size) of the base types the demo declares.
_BASE = {
    "BOOL": (sym.ADST_BIT, 1),
    "BYTE": (sym.ADST_UINT8, 1),
    "INT": (sym.ADST_INT16, 2),
    "UINT": (sym.ADST_UINT16, 2),
    "DINT": (sym.ADST_INT32, 4),
    "UDINT": (sym.ADST_UINT32, 4),
    "REAL": (sym.ADST_REAL32, 4),
    "LREAL": (sym.ADST_REAL64, 8),
    "STRING(31)": (sym.ADST_STRING, 32),
    "STRING(63)": (sym.ADST_STRING, 64),
}


def _type(
    name: str, type_name: str = "", size: int = 0, dtype: int = sym.ADST_BIGTYPE
) -> TypeEntry:
    return TypeEntry(name, type_name, size, 0, dtype, 0)


def _member(name: str, type_name: str, offset: int) -> TypeEntry:
    dtype, size = _BASE.get(type_name, (sym.ADST_BIGTYPE, 0))
    if type_name not in _BASE:
        size = {"ST_Motor": 16, "ARRAY [0..3] OF REAL": 16}[type_name]
    return TypeEntry(name, type_name, size, offset, dtype, 0)


def demo_types() -> dict[str, TypeEntry]:
    types = {name: _type(name, "", size, dtype) for name, (dtype, size) in _BASE.items()}
    motor = _type("ST_Motor", "", 16)
    motor.members = [
        _member("fSpeed", "LREAL", 0),
        _member("nRpm", "INT", 8),
        _member("bRunning", "BOOL", 10),
    ]
    temps = _type("ARRAY [0..3] OF REAL", "REAL", 16, sym.ADST_REAL32)
    temps.dims = [(0, 4)]
    app = _type("PlcAppSystemInfo", "", 72)
    app.members = [_member("OnlineChangeCnt", "UDINT", 0), _member("AppName", "STRING(63)", 8)]
    pointer = _type("POINTER TO BYTE", "BYTE", 8)
    types.update({t.name: t for t in (motor, temps, app, pointer)})
    return types


@dataclass
class _Var:
    entry: SymbolEntry
    data: bytearray


@dataclass
class _Sub:
    group: int
    offset: int
    size: int
    cycle: float
    callback: NotifyCallback
    last: bytes = b""
    due: float = 0.0


@dataclass
class DemoSlave:
    address: int
    vendor_id: int
    product_code: int
    revision: int
    al_state: int = 0x08
    link_state: int = 0x00
    # CoE objects by (index, subindex); None: no mailbox (couplers, digital terminals).
    coe: dict[tuple[int, int], bytes] | None = None


class DemoEtherCAT:
    """The demo's EtherCAT segment behind the master port: EK1100 + EL1004 + EL2004 +
    EL3004 in OP, one cyclic frame per task cycle, one acyclic frame per master read."""

    def __init__(self) -> None:
        self.al_state = 0x08
        self.cyclic = 0
        self.acyclic = 0
        self.slaves = [
            DemoSlave(1001, 2, 0x044C2C52, 0x00120000),
            DemoSlave(1002, 2, 0x03EC3052, 0x00100000),
            DemoSlave(1003, 2, 0x07D43052, 0x00100000),
            DemoSlave(
                1004,
                2,
                0x0BBC3052,
                0x00140000,
                coe={
                    (0x1008, 0): b"EL3004",
                    (0x1018, 1): struct.pack("<I", 2),
                    (0x1018, 2): struct.pack("<I", 0x0BBC3052),
                    (0x1018, 3): struct.pack("<I", 0x00140000),
                },
            ),
        ]
        self.slaves[-1].link_state = 0x20  # port B: end of the segment

    def step(self) -> None:
        self.cyclic = (self.cyclic + 1) & 0xFFFFFFFF

    def drop(self, address: int) -> None:
        """The slave at `address` falls off the bus."""
        slave = self._slave(address)
        slave.al_state, slave.link_state = 0x00, 0x01

    def _slave(self, address: int) -> DemoSlave:
        found = next((s for s in self.slaves if s.address == address), None)
        if found is None:
            raise ADSError(1796)  # invalid index offset
        return found

    def read(self, group: int, offset: int, size: int) -> bytes:
        """The master port's index groups, as TwinCAT lays them out."""
        self.acyclic = (self.acyclic + 1) & 0xFFFFFFFF
        if group == ecat.IG_MASTER_STATE and offset == ecat.IO_MASTER_STATE:
            data = struct.pack("<H", self.al_state)
        elif group == ecat.IG_SLAVE_COUNT:
            data = struct.pack("<H", len(self.slaves))
        elif group == ecat.IG_SLAVE_ADDRESSES:
            data = b"".join(struct.pack("<H", s.address) for s in self.slaves)
        elif group == ecat.IG_SLAVE_STATES and offset == 0:
            data = b"".join(bytes((s.al_state, s.link_state)) for s in self.slaves)
        elif group == ecat.IG_FRAME_COUNTERS:
            ticks = int(time.monotonic() * 1e7) & 0xFFFFFFFF  # 100 ns, 32-bit as TwinCAT
            data = struct.pack("<5I", ticks, self.cyclic, 0, self.acyclic, 0)
        elif group == ecat.IG_IDENTITY:
            s = self._slave(offset)
            data = struct.pack("<4I", s.vendor_id, s.product_code, s.revision, 0)
        elif group == ecat.IG_CRC:
            self._slave(offset)
            data = bytes(16)
        else:
            raise ADSError(1793)  # service not supported
        if size < len(data):
            raise ADSError(1797)  # parameter size not correct
        return data.ljust(size, b"\0")

    def read_port(self, ams_port: int, group: int, offset: int, size: int) -> bytes:
        """The I/O server's device list, or a slave's port: CoE SDO upload."""
        if ams_port == ecat.IO_PORT:
            return self._io_server(group, offset)[:size]
        slave = next((s for s in self.slaves if s.address == ams_port), None)
        if slave is None:
            raise ADSError(6)  # target port not found
        if group != ecat.IG_COE_SDO:
            raise ADSError(1793)
        if slave.coe is None:
            raise ADSError(ecat.ADS_PORT_NOT_CONNECTED)
        data = slave.coe.get((offset >> 16, offset & 0xFF))
        if data is None:
            raise ADSError(1796)
        return data[:size]

    @staticmethod
    def _io_server(group: int, offset: int) -> bytes:
        """Two I/O devices, as TwinCAT lists them: a non-EtherCAT one, then the master."""
        devices = {
            1: (0, b"Device 1 (Image)"),
            2: (ecat.IODEVICETYPE_ETHERCAT, b"Device 2 (EtherCAT)"),
        }
        if group == ecat.IG_IO_DEVICES and offset == ecat.IO_COUNT:
            return struct.pack("<I", len(devices))
        if group == ecat.IG_IO_DEVICES and offset == ecat.IO_IDS:
            return struct.pack(f"<{len(devices) + 1}H", len(devices), *devices)
        device = devices.get(group - ecat.IG_IO_DEVICE)
        if device is None:
            raise ADSError(1793)
        device_type, name = device
        if offset == ecat.IO_TYPE:
            return struct.pack("<H", device_type)
        if offset == ecat.IO_NAME:
            return name + b"\0"
        if offset == ecat.IO_NET_ID:
            return bytes(int(p) for p in DEMO_ECAT_NET_ID.split("."))
        raise ADSError(1796)


@dataclass
class DemoPlc:
    """The program: a cycle counter, a sine, two outputs, a motor struct, an array,
    a string, one GVL, a pointer (skipped) and TwinCAT's own system info (excluded)."""

    types: dict[str, TypeEntry] = field(default_factory=demo_types)
    vars: dict[str, _Var] = field(default_factory=dict)
    ethercat: DemoEtherCAT = field(default_factory=DemoEtherCAT)
    _subs: dict[int, _Sub] = field(default_factory=dict)
    _handles: dict[int, str] = field(default_factory=dict)  # symbol handle -> name
    _next: int = 1
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _thread: threading.Thread | None = None
    _stop: threading.Event = field(default_factory=threading.Event)
    _t0: float = 0.0
    _cycle: int = 0
    _offset: int = 0
    # The symbol version (0xF008): one byte TwinCAT bumps per online change.
    _version: _Var = field(
        default_factory=lambda: _Var(
            SymbolEntry("", "BYTE", ADSIGRP_SYM_VERSION, 0, 1, sym.ADST_UINT8, 0), bytearray([1])
        )
    )

    def __post_init__(self) -> None:
        declare = self._declare
        declare("MAIN.nCycle", "UDINT")
        declare("MAIN.fSine", "LREAL")
        declare("MAIN.bOut1", "BOOL", IG_OUTPUTS, "demo output 1")
        declare("MAIN.bOut2", "BOOL", IG_OUTPUTS, "demo output 2")
        declare("MAIN.stMotor", "ST_Motor")
        declare("MAIN.arrTemps", "ARRAY [0..3] OF REAL")
        declare("MAIN.sState", "STRING(31)")
        declare("MAIN.pBuffer", "POINTER TO BYTE")
        declare("GVL_IO.nPressure", "UINT")
        declare("GVL_IO.fFlow", "REAL")
        declare("TwinCAT_SystemInfoVarList._AppInfo", "PlcAppSystemInfo")
        self._poke("TwinCAT_SystemInfoVarList._AppInfo", 8, b"Demo".ljust(64, b"\0"))
        self.step(0.0)

    def _declare(self, name: str, type_name: str, group: int = IG_DATA, comment: str = "") -> None:
        dtype, size = _BASE.get(type_name, (sym.ADST_BIGTYPE, self.types[type_name].size))
        self.vars[name] = _Var(
            SymbolEntry(name, type_name, group, self._offset, size, dtype, 0, comment),
            bytearray(size),
        )
        self._offset += size

    def online_change(self) -> None:
        """A program download: one output more, laid out ahead of bOut1, so every
        variable from there moves; the symbol version moves with them."""
        with self._lock:
            self._declare("MAIN.bOut0", "BOOL", IG_OUTPUTS, "demo output 0")
            names = list(self.vars)
            names.insert(names.index("MAIN.bOut1"), names.pop())
            self.vars = {n: self.vars[n] for n in names}
            offset = 0
            for var in self.vars.values():
                var.entry.index_offset, offset = offset, offset + var.entry.size
            self._version.data[0] = (self._version.data[0] + 1) & 0xFF

    # ─── Program ────────────────────────────────────────────────────────────

    def _poke(self, name: str, offset: int, data: bytes) -> None:
        self.vars[name].data[offset : offset + len(data)] = data

    def step(self, t: float) -> None:
        """One task cycle at program time `t` (seconds)."""
        with self._lock:
            self._cycle += 1
            self.ethercat.step()
            self._poke("MAIN.nCycle", 0, struct.pack("<I", self._cycle & 0xFFFFFFFF))
            self._poke("MAIN.fSine", 0, struct.pack("<d", math.sin(t)))
            speed = 1500.0 + 500.0 * math.sin(t / 5.0)
            running = (int(t) // 20) % 2 == 0
            self._poke(
                "MAIN.stMotor",
                0,
                struct.pack(
                    "<dh?", speed if running else 0.0, int(speed) if running else 0, running
                ),
            )
            self._poke(
                "MAIN.arrTemps",
                0,
                struct.pack("<4f", *(20.0 + i + 2.0 * math.sin(t / 7.0 + i) for i in range(4))),
            )
            self._poke(
                "MAIN.sState", 0, sym.encode("RUNNING" if running else "IDLE", sym.ADST_STRING, 32)
            )
            self._poke(
                "GVL_IO.nPressure", 0, struct.pack("<H", int(1000 + 200 * math.sin(t / 3.0)))
            )
            self._poke("GVL_IO.fFlow", 0, struct.pack("<f", 12.5 + math.cos(t / 2.0)))

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._t0 = time.monotonic()
        self._thread = threading.Thread(target=self._run, name="demo-plc", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(1.0)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(TASK_SECONDS):
            now = time.monotonic()
            self.step(now - self._t0)
            self._notify(now)

    def _notify(self, now: float) -> None:
        with self._lock:
            due = [s for s in self._subs.values() if s.due <= now]
            for s in due:
                s.due = now + s.cycle
                data = bytes(self._slice(s.group, s.offset, s.size))
                if data == s.last:
                    continue
                s.last = data
                stamp = time.time_ns()
                s.callback(stamp, data)

    # ─── ADS surface ────────────────────────────────────────────────────────

    def _find(self, group: int, offset: int, size: int) -> tuple[_Var, int]:
        if group == ADSIGRP_SYM_VALBYHND:
            name = self._handles.get(offset)
            if name is None:
                raise ADSError(1809)  # symbol version invalid: no such handle
            if size > self.vars[name].entry.size:
                raise ADSError(1797)  # parameter size not correct
            return self.vars[name], 0
        for var in (*self.vars.values(), self._version):
            e = var.entry
            if (
                e.index_group == group
                and e.index_offset <= offset
                and offset + size <= e.index_offset + e.size
            ):
                return var, offset - e.index_offset
        raise ADSError(
            1794 if not any(v.entry.index_group == group for v in self.vars.values()) else 1795
        )

    def _slice(self, group: int, offset: int, size: int) -> bytearray:
        var, start = self._find(group, offset, size)
        return var.data[start : start + size]

    def symbol_table(self) -> bytes:
        return b"".join(encode_symbol(v.entry) for v in self.vars.values())

    def type_table(self) -> bytes:
        return b"".join(encode_type(t) for t in self.types.values())

    def read_state(self) -> tuple[int, int]:
        return ADSSTATE_RUN, 0

    def device_info(self) -> tuple[str, str]:
        return "Demo PLC", "3.1.4026"

    def read(self, group: int, offset: int, size: int) -> bytes:
        with self._lock:
            return bytes(self._slice(group, offset, size))

    def write(self, group: int, offset: int, data: bytes) -> None:
        with self._lock:
            if group == ADSIGRP_SYM_RELEASEHND:
                self.release_handle(struct.unpack("<I", data)[0])
                return
            var, start = self._find(group, offset, len(data))
            var.data[start : start + len(data)] = data

    def get_handle(self, name: str) -> int:
        """0xF003: a handle onto `name`, resolved now and by every access through it."""
        with self._lock:
            match = next((n for n in self.vars if n.lower() == name.lower()), None)
            if match is None:
                raise ADSError(1808)
            handle, self._next = self._next, self._next + 1
            self._handles[handle] = match
        return handle

    def release_handle(self, handle: int) -> None:
        with self._lock:
            if self._handles.pop(handle, None) is None:
                raise ADSError(1809)

    def sum_read(self, items: Sequence[tuple[int, int, int]]) -> list[bytes | int]:
        out: list[bytes | int] = []
        with self._lock:
            for group, offset, size in items:
                try:
                    out.append(bytes(self._slice(group, offset, size)))
                except ADSError as e:
                    out.append(e.err_code)
        return out

    def subscribe(
        self, group: int, offset: int, size: int, cycle_ms: int, callback: NotifyCallback
    ) -> int:
        # The current value goes out on the next task cycle, as TwinCAT sends one
        # on registration: `last` is empty so the first check sees a change.
        with self._lock:
            self._find(group, offset, size)
            handle, self._next = self._next, self._next + 1
            self._subs[handle] = _Sub(group, offset, size, cycle_ms / 1000.0, callback)
        return handle

    def unsubscribe(self, handle: int) -> None:
        with self._lock:
            if self._subs.pop(handle, None) is None:
                raise ADSError(1812)


def encode_symbol(e: SymbolEntry) -> bytes:
    name, type_name, comment = e.name.encode(), e.type_name.encode(), e.comment.encode()
    body = name + b"\0" + type_name + b"\0" + comment + b"\0"
    header = struct.pack(
        "<6I3H",
        30 + len(body),
        e.index_group,
        e.index_offset,
        e.size,
        e.data_type,
        e.flags,
        len(name),
        len(type_name),
        len(comment),
    )
    return header + body


def encode_type(t: TypeEntry) -> bytes:
    name, type_name = t.name.encode(), t.type_name.encode()
    body = name + b"\0" + type_name + b"\0" + b"\0"
    body += b"".join(struct.pack("<iI", lo, n) for lo, n in t.dims)
    body += b"".join(encode_type(m) for m in t.members)
    header = struct.pack(
        "<8I5H",
        42 + len(body),
        0,
        0,
        0,
        t.size,
        t.offset,
        t.data_type,
        t.flags,
        len(name),
        len(type_name),
        0,
        len(t.dims),
        len(t.members),
    )
    return header + body


class DemoLink:
    """A `Link` onto a `DemoPlc` in this process: the PLC runtime (851), or its
    EtherCAT master (`ecat.MASTER_PORT`), which answers reads and ReadState only."""

    def __init__(self, plc: DemoPlc, port: int = DEMO_PORT) -> None:
        self.plc = plc
        self.port = port
        self._open = False

    def open(self) -> None:
        self.plc.start()
        self._open = True

    def close(self) -> None:
        self._open = False

    def _require(self) -> DemoPlc:
        if not self._open:
            raise ADSError(1864)
        return self.plc

    def _require_plc(self) -> DemoPlc:
        plc = self._require()
        if self.port != DEMO_PORT:
            raise ADSError(1793)  # service not supported by the master port
        return plc

    def read_state(self) -> tuple[int, int]:
        plc = self._require()
        return plc.read_state() if self.port == DEMO_PORT else (0, 0)

    def device_info(self) -> tuple[str, str]:
        return self._require_plc().device_info()

    def upload(self) -> tuple[bytes, bytes]:
        plc = self._require_plc()
        return plc.symbol_table(), plc.type_table()

    def read(self, group: int, offset: int, size: int) -> bytes:
        plc = self._require()
        if self.port == ecat.MASTER_PORT:
            with plc._lock:
                return plc.ethercat.read(group, offset, size)
        return plc.read(group, offset, size)

    def read_port(self, ams_port: int, group: int, offset: int, size: int) -> bytes:
        plc = self._require()
        with plc._lock:
            return plc.ethercat.read_port(ams_port, group, offset, size)

    def write(self, group: int, offset: int, data: bytes) -> None:
        self._require_plc().write(group, offset, data)

    def write_by_name(self, name: str, data: bytes) -> bytes:
        plc = self._require_plc()
        handle = plc.get_handle(name)
        try:
            plc.write(ADSIGRP_SYM_VALBYHND, handle, data)
            return plc.read(ADSIGRP_SYM_VALBYHND, handle, len(data))
        finally:
            plc.release_handle(handle)

    def sum_read(self, items: Sequence[tuple[int, int, int]]) -> list[bytes | int]:
        return self._require_plc().sum_read(items)

    def subscribe(
        self, group: int, offset: int, size: int, cycle_ms: int, callback: NotifyCallback
    ) -> int:
        return self._require_plc().subscribe(group, offset, size, cycle_ms, callback)
