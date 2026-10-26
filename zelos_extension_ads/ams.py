"""AMS plumbing: NetIds, the local router, Beckhoff's UDP 48899 identify and
route handshake.

Two platform modes, decided by pyads at import:

- router-less (Linux, macOS): pyads bundles Beckhoff's AdsLib, which is its own
  router. The process picks a local AMS NetId and adds one route per target IP;
  the target's router must hold a route back to this NetId (`add_route`).
- native router (Beckhoff RT Linux, Windows with TwinCAT): pyads loads the
  TwinCAT ADS DLL and the device's own router carries the connection. A local
  target needs nothing; a remote target needs a route in that router (XAE or
  `adstool addroute`), which this extension cannot add.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import struct
import threading
from dataclasses import dataclass

import ifaddr
import pyads
from pyads.ads import linux as ROUTERLESS

logger = logging.getLogger(__name__)

PLC_PORT = 851
ADS_TCP_PORT = 48898
UDP_PORT = 48899
SYSTEM_SERVICE_PORT = 10000

#: Beckhoff's UDP frame: magic, 4 reserved, command (1 identify, 6 add route).
_UDP_MAGIC = b"\x03\x66\x14\x71"
_CMD_IDENTIFY = 1
_CMD_ADD_ROUTE = 6
_RESPONSE = 0x80  # command byte 3 in a reply
# Tagged blocks: identify replies carry hostname and version; add-route requests the rest.
_TAG_ROUTE_NAME = 0x0C
_TAG_NET_ID = 0x07
_TAG_USER = 0x0D
_TAG_PASSWORD = 0x02
_TAG_HOSTNAME = 5
_TAG_VERSION = 3
_TAG_RESULT = 1  # add-route reply: u32 ADS result
#: ADSERR_DEVICE_ACCESSDENIED (0x704): the target refused the credentials.
ADS_ROUTE_REFUSED = 1796

_router_lock = threading.Lock()
_local_net_id: str | None = None


def parse_net_id(value: str) -> str:
    """`value` as an AMS NetId (six 0-255 numbers), or ValueError."""
    parts = value.strip().split(".")
    if len(parts) != 6 or not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        raise ValueError(f"'{value}' is not an AMS NetId (six numbers, e.g. 5.1.2.3.1.1)")
    return ".".join(str(int(p)) for p in parts)


def outbound_ip(host: str) -> str:
    """This machine's IP on the interface that reaches `host`; no packet is sent."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((host, ADS_TCP_PORT))
        return sock.getsockname()[0]


def native_router() -> bool:
    """Whether the TwinCAT router on this machine carries ADS (no local NetId or routes)."""
    return not ROUTERLESS


def local_net_id() -> str:
    """The NetId this process presents, once `ensure_local_router` ran; else ""."""
    return _local_net_id or ""


def ensure_local_router(host: str, local_net_id: str = "") -> str:
    """Prepare the local side, once per process: on a router-less platform open the
    AdsLib port and fix its NetId (`local_net_id`, else Beckhoff's convention: this
    machine's IP toward `host` plus `.1.1`); nothing on a TwinCAT router. Returns the
    NetId in use.

    AdsLib holds one NetId per process, so it is resolved before any target
    connects, from the first one; a later different one is logged, not applied.
    The target's router matches a route by the sender's NetId and TCP source
    address, so the NetId is derived from the interface that reaches it.
    """
    global _local_net_id
    if native_router():
        return ""
    net_id = parse_net_id(local_net_id) if local_net_id else outbound_ip(host) + ".1.1"
    with _router_lock:
        if _local_net_id is None:
            pyads.open_port()
            pyads.set_local_address(net_id)
            _local_net_id = net_id
            logger.info("Local AMS NetId: %s", net_id)
        elif _local_net_id != net_id:
            logger.warning(
                "Local AMS NetId is %s for this process; %s not applied", _local_net_id, net_id
            )
        return _local_net_id


@dataclass
class Device:
    """A TwinCAT router that answered a UDP identify."""

    host: str  # the address that answered
    net_id: str
    name: str = ""
    version: str = ""  # TwinCAT, e.g. 3.1.4026


def _identify_frame() -> bytes:
    # The sender NetId in the frame is irrelevant to the reply.
    return (
        _UDP_MAGIC
        + b"\x00\x00\x00\x00"
        + struct.pack("<I", _CMD_IDENTIFY)
        + bytes((1, 1, 1, 1, 1, 1))
        + struct.pack("<H", SYSTEM_SERVICE_PORT)
        + b"\x00\x00\x00\x00"
    )


def parse_identify(data: bytes, host: str) -> Device | None:
    """A Device from an identify reply, or None for anything else."""
    if (
        len(data) < 24
        or data[:4] != _UDP_MAGIC
        or data[8:12] != bytes((_CMD_IDENTIFY, 0, 0, _RESPONSE))
    ):
        return None
    device = Device(host=host, net_id=".".join(str(b) for b in data[12:18]))
    count = struct.unpack_from("<I", data, 20)[0]
    offset = 24
    for _ in range(count):
        if offset + 4 > len(data):
            break
        tag, length = struct.unpack_from("<HH", data, offset)
        offset += 4
        value, offset = data[offset : offset + length], offset + length
        if tag == _TAG_HOSTNAME:
            device.name = value.split(b"\0", 1)[0].decode(errors="replace")
        elif tag == _TAG_VERSION and length >= 4:
            major, minor, build = struct.unpack_from("<BBH", value)
            device.version = f"{major}.{minor}.{build}"
    return device


def broadcast_addresses() -> list[str]:
    """The limited broadcast address, then each IPv4 interface's own: the limited one
    leaves by the default route only, missing a PLC on a second interface."""
    out = ["255.255.255.255"]
    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            if not isinstance(ip.ip, str) or ip.ip.startswith("127.") or ip.network_prefix >= 31:
                continue
            net = ipaddress.IPv4Network(f"{ip.ip}/{ip.network_prefix}", strict=False)
            if str(net.broadcast_address) not in out:
                out.append(str(net.broadcast_address))
    return out


def identify(hosts: list[str], timeout: float = 2.0, broadcast: bool = False) -> list[Device]:
    """Routers that answer a UDP identify within `timeout`: each of `hosts`, and with
    `broadcast` every broadcast address too. Nothing else is sent."""
    found: dict[str, Device] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("", 0))
        sock.settimeout(timeout)
        frame = _identify_frame()
        if broadcast:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            for address in broadcast_addresses():
                try:
                    sock.sendto(frame, (address, UDP_PORT))
                except OSError as e:  # no broadcast-capable interface
                    logger.debug("broadcast to %s failed: %s", address, e)
        waiting: set[str] = set()  # the addresses an answer is still expected from
        for host in hosts:
            try:
                sock.sendto(frame, (host, UDP_PORT))
                waiting.add(socket.gethostbyname(host))
            except OSError as e:  # unresolvable name
                logger.debug("identify %s: %s", host, e)
        while broadcast or waiting:
            try:
                data, (address, _) = sock.recvfrom(2048)
            except TimeoutError:
                break
            except OSError:  # a refused unicast surfaces here on some platforms
                continue
            device = parse_identify(data, address)
            if device is not None and device.net_id not in found:
                found[device.net_id] = device
            waiting.discard(address)
    return list(found.values())


def add_route(
    host: str,
    username: str,
    password: str,
    local_net_id: str,
    local_host: str,
    route_name: str,
    timeout: float = 5.0,
) -> bool:
    """Add a route for `local_net_id` at `local_host` to the TwinCAT router at `host`
    through Beckhoff's UDP handshake; False when the credentials are refused (ADS 1796),
    RuntimeError naming any other ADS result.

    The credentials go into one UDP frame and are kept nowhere. The frame is the
    one pyads's `add_route_to_plc` sends, built here because that function misreads
    a refusal from TwinCAT 3.1.4026 (tag 1 value 0x0704, ADS 1796) as an unexpected
    reply and raises instead of returning False. The reply is read as AdsLib reads it.
    """

    def block(tag: int, text: str) -> bytes:
        value = text.encode() + b"\0"
        return struct.pack("<HH", tag, len(value)) + value

    net_id = bytes(int(p) for p in parse_net_id(local_net_id).split("."))
    frame = (
        _UDP_MAGIC
        + b"\x00\x00\x00\x00"
        + struct.pack("<I", _CMD_ADD_ROUTE)
        + net_id
        + struct.pack("<H", SYSTEM_SERVICE_PORT)
        + struct.pack("<I", 5)  # block count
        + block(_TAG_ROUTE_NAME, route_name)
        + struct.pack("<HH", _TAG_NET_ID, 6)
        + net_id
        + block(_TAG_USER, username)
        + block(_TAG_PASSWORD, password)
        + block(_TAG_HOSTNAME, local_host)
    )
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("", 0))
        sock.settimeout(timeout)
        sock.sendto(frame, (host, UDP_PORT))
        data, _ = sock.recvfrom(1024)
    code = parse_add_route_reply(data)
    if code is None:
        raise RuntimeError(f"unexpected reply from {host}: {data.hex()}")
    if code == 0:
        return True
    if code == ADS_ROUTE_REFUSED:
        return False
    raise RuntimeError(f"{host} did not add the route: ADS error {code} (0x{code:X})")


def parse_add_route_reply(data: bytes) -> int | None:
    """The ADS result of an add-route reply (tag 1, a u32), None if malformed.

    As Beckhoff's AdsLib `AddRemoteRoute`: other tags are skipped; tag 1 must be 4 bytes.
    """
    if (
        len(data) < 24
        or data[:4] != _UDP_MAGIC
        or data[8:12] != bytes((_CMD_ADD_ROUTE, 0, 0, _RESPONSE))
    ):
        return None
    count = struct.unpack_from("<I", data, 20)[0]
    offset = 24
    for _ in range(count):
        if offset + 4 > len(data):
            return None
        tag, length = struct.unpack_from("<HH", data, offset)
        offset += 4
        if tag == _TAG_RESULT:
            if length != 4 or offset + 4 > len(data):
                return None
            return struct.unpack_from("<I", data, offset)[0]
        offset += length
    return None
