"""The one ADS link an `ADSClient` drives: pyads to a real target, or the demo PLC.

Every method blocks and is called from the client's worker thread, never the
event loop. Notification callbacks arrive on the library's own receive thread.
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import struct
from collections.abc import Callable, Sequence
from functools import partial
from typing import Any, Protocol

import pyads
from pyads import pyads_ex
from pyads.ads import linux as ROUTERLESS
from pyads.pyads_ex import ADSError
from pyads.structs import SAdsSumRequest

logger = logging.getLogger(__name__)

# ADS index groups.
ADSIGRP_SYM_HNDBYNAME = 0xF003  # name -> handle
ADSIGRP_SYM_VALBYHND = 0xF005  # value by handle (index offset)
ADSIGRP_SYM_RELEASEHND = 0xF006
ADSIGRP_SYM_VERSION = 0xF008  # one byte TwinCAT bumps per download, not online change
ADSIGRP_SYM_UPLOAD = 0xF00B
ADSIGRP_SYM_DT_UPLOAD = 0xF00E
ADSIGRP_SYM_UPLOADINFO2 = 0xF00F
ADSIGRP_SUMUP_READ = 0xF080
# Sub-requests per sum read: Beckhoff's recommended ceiling, not a frame limit; pyads
# uses the same chunk.
SUM_CHUNK = 500

# Windows FILETIME (100 ns since 1601) -> Unix ns.
FILETIME_EPOCH = 116444736000000000

#: A notification's data and its PLC timestamp (Unix ns).
NotifyCallback = Callable[[int, bytes], None]


class Link(Protocol):
    """What the client needs from a target; see `PyadsLink` and `demo.DemoLink`."""

    def open(self) -> None: ...
    def close(self) -> None: ...
    def read_state(self) -> tuple[int, int]: ...
    def device_info(self) -> tuple[str, str]: ...
    def upload(self) -> tuple[bytes, bytes]: ...
    def read(self, group: int, offset: int, size: int) -> bytes: ...
    def write(self, group: int, offset: int, data: bytes) -> None: ...
    def write_by_name(self, name: str, data: bytes) -> bytes: ...
    def sum_read(self, items: Sequence[tuple[int, int, int]]) -> list[bytes | int]: ...
    def subscribe(
        self, group: int, offset: int, size: int, cycle_ms: int, callback: NotifyCallback
    ) -> int: ...
    def read_port(self, ams_port: int, group: int, offset: int, size: int) -> bytes: ...


def filetime_ns(filetime: int) -> int:
    return (filetime - FILETIME_EPOCH) * 100


class PyadsLink:
    """One pyads connection to one AMS port of one target."""

    def __init__(self, host: str, net_id: str, port: int, timeout: float) -> None:
        self.host, self.net_id, self.port, self.timeout = host, net_id, port, timeout
        self._plc: pyads.Connection | None = None
        self._handles: set[int] = set()  # notifications registered on the target

    def open(self) -> None:
        plc = pyads.Connection(self.net_id, self.port, self.host)
        plc.open()
        plc.set_timeout(int(self.timeout * 1000))
        self._plc = plc

    def close(self) -> None:
        """Close the port, then drop the route.

        pyads's own `close()` drops the route first; with notifications still
        registered on a dead link, AdsLib then dereferences the connection the
        route owned while closing the port (segfault, reproduced on 3.6.0).
        Closing the port first deletes the notifications on a live link and
        merely times out on a dead one.
        """
        plc, self._plc = self._plc, None
        if plc is None:
            return
        handles, self._handles = self._handles, set()
        port, plc._port, plc._open = plc._port, None, False
        if port is not None:
            with contextlib.suppress(ADSError, OSError):
                pyads_ex.adsPortCloseEx(port)
        # pyads keeps every notification's C callback in a process-wide store that only
        # `del_device_notification` prunes; the port is closed, so no callback can fire.
        for handle in handles:
            pyads_ex.callback_store.pop((plc._adr, handle), None)
        if ROUTERLESS:
            with contextlib.suppress(ADSError, OSError):
                pyads_ex.adsDelRoute(plc._adr.netIdStruct())

    def _require(self) -> pyads.Connection:
        if self._plc is None:
            raise ADSError(1864)  # port not opened
        return self._plc

    def read_state(self) -> tuple[int, int]:
        state = self._require().read_state()
        return (int(state[0]), int(state[1])) if state else (0, 0)

    def device_info(self) -> tuple[str, str]:
        info = self._require().read_device_info()
        if not info:
            return "", ""
        name, version = info
        return str(name), f"{version.version}.{version.revision}.{version.build}"

    def read(self, group: int, offset: int, size: int) -> bytes:
        buf = self._require().read(
            group, offset, ctypes.c_ubyte * size, return_ctypes=True, check_length=False
        )
        return bytes(buf)

    def write(self, group: int, offset: int, data: bytes) -> None:
        self._require().write(group, offset, data, ctypes.c_ubyte * len(data))

    def write_by_name(self, name: str, data: bytes) -> bytes:
        """Write through a symbol handle and read back through the same one: the
        target resolves `name` now, whatever the uploaded table says.

        pyads's handle-less `write_by_name` leaks the handle when the write raises;
        this holds it in a `finally`.
        """
        plc = self._require()
        buf = ctypes.c_ubyte * len(data)
        handle = plc.get_handle(name)
        try:
            plc.write_by_name("", data, buf, handle=handle)
            return bytes(
                plc.read_by_name("", buf, return_ctypes=True, handle=handle, check_length=False)
            )
        finally:
            plc.release_handle(handle)

    def upload(self) -> tuple[bytes, bytes]:
        info = self.read(ADSIGRP_SYM_UPLOADINFO2, 0, 24)
        _, sym_size, _, dt_size = struct.unpack_from("<4I", info)
        symbols = self.read(ADSIGRP_SYM_UPLOAD, 0, sym_size) if sym_size else b""
        types = self.read(ADSIGRP_SYM_DT_UPLOAD, 0, dt_size) if dt_size else b""
        return symbols, types

    def sum_read(self, items: Sequence[tuple[int, int, int]]) -> list[bytes | int]:
        """Each item's bytes, or its ADS error code."""
        out: list[bytes | int] = []
        plc = self._require()
        for start in range(0, len(items), SUM_CHUNK):
            chunk = items[start : start + SUM_CHUNK]
            # pyads sizes the reply from these entries' `size`.
            requests = (SAdsSumRequest * len(chunk))()
            for request, (group, offset, size) in zip(requests, chunk, strict=True):
                request.iGroup, request.iOffset, request.size = group, offset, size
            response = bytes(
                plc.read_write(
                    ADSIGRP_SUMUP_READ, len(chunk), None, requests, None, return_ctypes=True
                )
            )
            offset = 4 * len(chunk)
            for i, (_, _, item_size) in enumerate(chunk):
                error = struct.unpack_from("<I", response, 4 * i)[0]
                out.append(error or response[offset : offset + item_size])
                offset += item_size
        return out

    def subscribe(
        self, group: int, offset: int, size: int, cycle_ms: int, callback: NotifyCallback
    ) -> int:
        plc = self._require()
        attrib = pyads.NotificationAttrib(size, pyads.ADSTRANS_SERVERONCHA, cycle_ms, cycle_ms)
        handle, _ = plc.add_device_notification(
            (group, offset), attrib, partial(self._dispatch, callback)
        )
        self._handles.add(handle)
        return handle

    def read_port(self, ams_port: int, group: int, offset: int, size: int) -> bytes:
        """Read from another AMS port of the same device over this link's port and
        route: up to `size` bytes, as many as the device returned."""
        plc = self._require()
        address = pyads.AmsAddr(self.net_id, ams_port).amsAddrStruct()
        data = (ctypes.c_ubyte * size)()
        read = ctypes.c_ulong()
        error = pyads_ex._adsDLL.AdsSyncReadReqEx2(
            plc._port,
            ctypes.pointer(address),
            ctypes.c_ulong(group),
            ctypes.c_ulong(offset),
            ctypes.c_ulong(size),
            ctypes.pointer(data),
            ctypes.pointer(read),
        )
        if error:
            raise ADSError(error)
        return bytes(data)[: read.value]

    @staticmethod
    def _dispatch(callback: NotifyCallback, notification: Any, _data: Any) -> None:
        header = notification.contents
        data = ctypes.string_at(
            ctypes.addressof(header) + type(header).data.offset, header.cbSampleSize
        )
        callback(filetime_ns(header.nTimeStamp), data)


def describe_error(error: BaseException) -> str:
    """One line for a log or an action result."""
    if isinstance(error, ADSError):
        return error.msg.rstrip(". ")
    return str(error) or error.__class__.__name__


# Router (1-31) and client (1856+) codes: the link, not one request.
def is_connection_error(error: BaseException) -> bool:
    if isinstance(error, ADSError):
        code = getattr(error, "err_code", None)
        return code is None or code < 1792 or code >= 1856
    return isinstance(error, OSError | RuntimeError)


# The addresses are stale (online change, new program): upload the table again.
RESYNC_CODES = frozenset({1794, 1795, 1808, 1809, 1812, 1826})


def needs_resync(error: BaseException) -> bool:
    return isinstance(error, ADSError) and getattr(error, "err_code", None) in RESYNC_CODES
