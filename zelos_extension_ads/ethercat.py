"""EtherCAT diagnostics through the TwinCAT EtherCAT master's own ADS interface.

The master is an ADS device of its own: the runtime's AMS NetId with the fifth
number set to 2 (the first EtherCAT device; XAE shows it on the device's
EtherCAT tab, the PLC as `InfoData.AmsNetId`), AMS port 0xFFFF. Each slave
answers CoE on the port equal to its station address (1001...).

Every function here runs on the client's worker thread, on a `Link` to the
master device.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any

from zelos_sdk import DataType

from zelos_extension_ads.symbols import sanitize
from zelos_extension_ads.transport import Link

MASTER_PORT = 0xFFFF
# Index groups on the master port (Beckhoff Infosys, EtherCAT master ADS interface).
IG_MASTER_STATE = 0x3  # offset 0x100: u16 state
IG_SLAVE_COUNT = 0x6  # u32 projected slaves
IG_SLAVE_ADDRESSES = 0x7  # u16 station address per slave
IG_SLAVE_STATES = 0x9  # 2 bytes per slave: EtherCAT state, link status
IG_FRAME_COUNTERS = (
    0xC  # 5 x u32: system time, cyclic sent, cyclic lost, acyclic sent, acyclic lost
)
IG_IDENTITY = 0x11  # offset = station address: 4 x u32 vendor, product, revision, serial
IG_CRC = 0x12  # offset = station address: 4 x u32, ports A-D
IO_MASTER_STATE = 0x100
# On the slave's port: CoE SDO upload, offset = index << 16 | subindex.
IG_COE_SDO = 0xF302
#: Longest SDO payload asked for; the master returns the object's actual length.
SDO_MAX = 256
#: Port not connected: the slave has no mailbox (digital terminals, couplers).
ADS_PORT_NOT_CONNECTED = 13

MASTER_EVENT = "_ethercat"

AL_STATE_NAMES = {0: "NONE", 1: "INIT", 2: "PREOP", 3: "BOOT", 4: "SAFEOP", 8: "OP"}
ERROR_FLAG = 0x10
LINK_BITS = (
    (0x01, "not present"),
    (0x02, "no communication"),
    (0x04, "missing link"),
    (0x08, "additional link"),
    (0x10, "port A"),
    (0x20, "port B"),
    (0x40, "port C"),
    (0x80, "port D"),
)
#: Beckhoff product codes: terminal number in the high word, family in the low.
BECKHOFF = 2
FAMILIES = {0x3052: "EL", 0x2C52: "EK", 0x4052: "EP"}

MASTER_FIELDS = [
    ("slaves", DataType.UInt16, ""),
    ("al_state", DataType.UInt8, ""),
    ("al_state_name", DataType.String, ""),
    ("cyclic_frames", DataType.UInt32, ""),
    ("lost_frames", DataType.UInt32, ""),
    ("acyclic_frames", DataType.UInt32, ""),
    ("lost_acyclic_frames", DataType.UInt32, ""),
]
SLAVE_FIELDS = [
    ("al_state", DataType.UInt8, ""),
    ("al_state_name", DataType.String, ""),
    ("link_state", DataType.UInt8, ""),
    ("link_state_name", DataType.String, ""),
    ("lost", DataType.Boolean, ""),
    ("crc_port_a", DataType.UInt32, ""),
    ("crc_port_b", DataType.UInt32, ""),
    ("crc_port_c", DataType.UInt32, ""),
    ("crc_port_d", DataType.UInt32, ""),
    ("address", DataType.UInt16, ""),
    ("vendor_id", DataType.UInt32, ""),
    ("product_code", DataType.UInt32, ""),
    ("revision", DataType.UInt32, ""),
    ("serial", DataType.UInt32, ""),
]

SDO_TYPES = ("auto", "u8", "u16", "u32", "u64", "i8", "i16", "i32", "i64", "string", "hex")
_SDO_FORMATS = {
    "u8": "<B", "u16": "<H", "u32": "<I", "u64": "<Q",
    "i8": "<b", "i16": "<h", "i32": "<i", "i64": "<q",
}  # fmt: skip


def ethercat_net_id(runtime_net_id: str) -> str:
    """The first EtherCAT device's NetId by TwinCAT's convention: `a.b.c.d.2.1`."""
    return ".".join(runtime_net_id.split(".")[:4] + ["2", "1"])


def al_state_name(state: int) -> str:
    base = AL_STATE_NAMES.get(state & 0x0F, f"0x{state & 0x0F:02X}")
    return f"{base}+ERROR" if state & ERROR_FLAG else base


def link_state_name(link: int) -> str:
    return "|".join(name for bit, name in LINK_BITS if link & bit) or "ok"


def product_name(vendor_id: int, product_code: int) -> str:
    """`EL2004` for a Beckhoff terminal, else the product code in hex."""
    family = FAMILIES.get(product_code & 0xFFFF)
    if vendor_id == BECKHOFF and family:
        return f"{family}{product_code >> 16}"
    return f"0x{product_code:08x}"


@dataclass
class Slave:
    position: int  # 1-based bus position, as TwinCAT numbers terminals
    address: int
    vendor_id: int
    product_code: int
    revision: int
    serial: int
    al_state: int = 0
    link_state: int = 0
    crc: tuple[int, int, int, int] = (0, 0, 0, 0)

    @property
    def name(self) -> str:
        return product_name(self.vendor_id, self.product_code)

    @property
    def trace_name(self) -> str:
        """`2_EL2004`: bus position keeps the name unique and in bus order."""
        return f"{self.position}_{sanitize(self.name, 'event')}"

    @property
    def event(self) -> str:
        return f"{MASTER_EVENT}/{self.trace_name}"

    def fields(self) -> dict[str, Any]:
        return {
            "al_state": self.al_state,
            "al_state_name": al_state_name(self.al_state),
            "link_state": self.link_state,
            "link_state_name": link_state_name(self.link_state),
            "lost": bool(self.link_state & 0x01),
            "crc_port_a": self.crc[0],
            "crc_port_b": self.crc[1],
            "crc_port_c": self.crc[2],
            "crc_port_d": self.crc[3],
            "address": self.address,
            "vendor_id": self.vendor_id,
            "product_code": self.product_code,
            "revision": self.revision,
            "serial": self.serial,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "name": self.name,
            "slave": self.trace_name,
            **self.fields(),
        }


@dataclass
class Snapshot:
    al_state: int
    cyclic_frames: int
    lost_frames: int
    acyclic_frames: int
    lost_acyclic_frames: int
    slaves: list[Slave] = field(default_factory=list)

    def fields(self) -> dict[str, Any]:
        return {
            "slaves": len(self.slaves),
            "al_state": self.al_state,
            "al_state_name": al_state_name(self.al_state),
            "cyclic_frames": self.cyclic_frames,
            "lost_frames": self.lost_frames,
            "acyclic_frames": self.acyclic_frames,
            "lost_acyclic_frames": self.lost_acyclic_frames,
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self.fields(), "slaves": [s.as_dict() for s in self.slaves]}

    def find(self, selector: str) -> Slave:
        """A slave by position (`2`), trace name (`2_EL2004`) or station address (`1002`)."""
        text = selector.strip()
        if not text:
            raise ValueError("slave is required: a position, name or address from ethercat_slaves")
        for s in self.slaves:
            if text in (str(s.position), str(s.address), s.trace_name):
                return s
        names = ", ".join(s.trace_name for s in self.slaves)
        raise ValueError(f"No EtherCAT slave '{text}'; slaves: {names or 'none'}")


def read_master_state(link: Link) -> int:
    return struct.unpack("<H", link.read(IG_MASTER_STATE, IO_MASTER_STATE, 2))[0]


def read_snapshot(link: Link, identities: dict[int, tuple[int, int, int, int]]) -> Snapshot:
    """Worker thread: master state and counters, then every slave's state, link and CRC.

    `identities` caches the identity object per station address across reads
    (static, read once per address per connection).
    """
    al_state = read_master_state(link)
    _, cyclic, lost, acyclic, lost_acyclic = struct.unpack(
        "<5I", link.read(IG_FRAME_COUNTERS, 0, 20)
    )
    snapshot = Snapshot(al_state, cyclic, lost, acyclic, lost_acyclic)
    count = struct.unpack("<I", link.read(IG_SLAVE_COUNT, 0, 4))[0]
    if not count:
        return snapshot
    addresses = struct.unpack(f"<{count}H", link.read(IG_SLAVE_ADDRESSES, 0, 2 * count))
    states = link.read(IG_SLAVE_STATES, 0, 2 * count)
    for i, address in enumerate(addresses):
        identity = identities.get(address)
        if identity is None:
            identity = struct.unpack("<4I", link.read(IG_IDENTITY, address, 16))
            identities[address] = identity
        slave = Slave(i + 1, address, *identity)
        slave.al_state, slave.link_state = states[2 * i], states[2 * i + 1]
        slave.crc = struct.unpack("<4I", link.read(IG_CRC, address, 16))
        snapshot.slaves.append(slave)
    return snapshot


def parse_index(text: str) -> int:
    """`0x1018` or `4120` as a CoE index."""
    try:
        value = int(text.strip(), 0)
    except ValueError as e:
        raise ValueError(f"index '{text}' is not a number (e.g. 0x1018)") from e
    if not 0 <= value <= 0xFFFF:
        raise ValueError(f"index {text} is outside 0..0xFFFF")
    return value


def read_sdo(link: Link, address: int, index: int, subindex: int) -> bytes:
    """Worker thread: CoE SDO upload through the master, on the slave's port."""
    return link.read_port(address, IG_COE_SDO, (index << 16) | subindex, SDO_MAX)


def decode_sdo(data: bytes, data_type: str) -> tuple[str, Any]:
    """(type used, value); `auto` picks an unsigned integer by length, else hex."""
    if data_type == "auto":
        data_type = {1: "u8", 2: "u16", 4: "u32", 8: "u64"}.get(len(data), "hex")
    if data_type == "string":
        return data_type, data.split(b"\0", 1)[0].decode("latin-1")
    fmt = _SDO_FORMATS.get(data_type)
    if fmt is None or struct.calcsize(fmt) != len(data):
        return "hex", data.hex()
    return data_type, struct.unpack(fmt, data)[0]
