"""The demo PLC behind AMS/TCP, so a real ADS client (this extension through pyads,
or any other) can talk to it on 127.0.0.1:48898.

AdsLib connects to a route's IP on 48898 only, so the port is fixed. One
runtime port (851) answers; anything else is ADS error 6, as on a TwinCAT
without that runtime.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
from dataclasses import dataclass, field

from pyads.pyads_ex import ADSError

from zelos_extension_ads.ams import ADS_TCP_PORT
from zelos_extension_ads.demo.plc import DEMO_HOST, DEMO_NET_ID, DEMO_PORT, DemoPlc
from zelos_extension_ads.transport import (
    ADSIGRP_SUMUP_READ,
    ADSIGRP_SYM_DT_UPLOAD,
    ADSIGRP_SYM_UPLOAD,
    ADSIGRP_SYM_UPLOADINFO2,
    FILETIME_EPOCH,
)

logger = logging.getLogger(__name__)

# ADS command ids.
READ_DEVICE_INFO, READ, WRITE, READ_STATE = 1, 2, 3, 4
ADD_NOTIFICATION, DEL_NOTIFICATION, NOTIFICATION, READ_WRITE = 6, 7, 8, 9
STATE_REQUEST, STATE_RESPONSE = 0x0004, 0x0005
_AMS_HEADER = struct.Struct(
    "<6sH6sHHHIII"
)  # target, port, source, port, cmd, state, len, err, invoke


def net_id_bytes(net_id: str) -> bytes:
    return bytes(int(p) for p in net_id.split("."))


@dataclass
class _Client:
    writer: asyncio.StreamWriter
    net_id: bytes = b""
    port: int = 0
    handles: set[int] = field(default_factory=set)  # notifications held


class DemoServer:
    """AMS/TCP front of one `DemoPlc`."""

    def __init__(
        self, plc: DemoPlc | None = None, host: str = DEMO_HOST, port: int = ADS_TCP_PORT
    ) -> None:
        self.plc = plc or DemoPlc()
        self.host, self.port = host, port
        self.net_id = net_id_bytes(DEMO_NET_ID)
        self._server: asyncio.AbstractServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self.requests = 0

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._server = await asyncio.start_server(self._serve, self.host, self.port)
        self.plc.start()
        logger.info(
            "Demo PLC %s on %s:%d, AMS port %d", DEMO_NET_ID, self.host, self.port, DEMO_PORT
        )

    async def stop(self) -> None:
        """Stop listening and drop every client, as a TwinCAT restart does."""
        self.plc.stop()
        if self._server is not None:
            self._server.close()
            for writer in list(self._writers):
                writer.close()
            await self._server.wait_closed()
            self._server = None

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = _Client(writer)
        self._writers.add(writer)
        try:
            while True:
                head = await reader.readexactly(6)
                length = struct.unpack("<xxI", head)[0]
                frame = await reader.readexactly(length)
                self.requests += 1
                reply = self._handle(client, frame)
                if reply:
                    writer.write(reply)
                    await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            for handle in list(client.handles):
                with contextlib.suppress(ADSError):
                    self.plc.unsubscribe(handle)
            self._writers.discard(writer)
            writer.close()

    def _handle(self, client: _Client, frame: bytes) -> bytes:
        target, tport, source, sport, cmd, state, _, _, invoke = _AMS_HEADER.unpack_from(frame)
        data = frame[_AMS_HEADER.size :]
        client.net_id, client.port = source, sport
        if state & 1:
            return b""  # a response to us (none expected)
        if tport != DEMO_PORT:
            result, body = 6, b""  # target port not found
        else:
            try:
                result, body = 0, self._dispatch(client, cmd, data)
            except ADSError as e:
                result, body = e.err_code, b""
        payload = struct.pack("<I", result) + body
        header = _AMS_HEADER.pack(
            source, sport, self.net_id, DEMO_PORT, cmd, STATE_RESPONSE, len(payload), 0, invoke
        )
        return struct.pack("<xxI", len(header) + len(payload)) + header + payload

    def _dispatch(self, client: _Client, cmd: int, data: bytes) -> bytes:
        plc = self.plc
        if cmd == READ_DEVICE_INFO:
            name, version = plc.device_info()
            major, minor, build = (int(p) for p in version.split("."))
            return struct.pack("<BBH", major, minor, build) + name.encode().ljust(16, b"\0")[:16]
        if cmd == READ_STATE:
            return struct.pack("<HH", *plc.read_state())
        if cmd == READ:
            group, offset, size = struct.unpack_from("<3I", data)
            value = self._read(group, offset, size)
            return struct.pack("<I", len(value)) + value
        if cmd == WRITE:
            group, offset, size = struct.unpack_from("<3I", data)
            plc.write(group, offset, data[12 : 12 + size])
            return b""
        if cmd == READ_WRITE:
            group, offset, read_size, write_size = struct.unpack_from("<4I", data)
            payload = data[16 : 16 + write_size]
            if group == ADSIGRP_SUMUP_READ:
                items = [struct.unpack_from("<3I", payload, 12 * i) for i in range(offset)]
                results = plc.sum_read(items)
                value = b"".join(struct.pack("<I", r if isinstance(r, int) else 0) for r in results)
                value += b"".join(
                    r if isinstance(r, bytes) else bytes(item[2])
                    for r, item in zip(results, items, strict=True)
                )
                return struct.pack("<I", len(value)) + value
            value = self._read(group, offset, read_size)
            return struct.pack("<I", len(value)) + value
        if cmd == ADD_NOTIFICATION:
            group, offset, size, _mode, _delay, cycle = struct.unpack_from("<6I", data)
            box: list[int] = []
            # Under the PLC lock: the task cannot notify before the handle is known.
            with plc._lock:
                handle = plc.subscribe(
                    group, offset, size, max(cycle // 10000, 1), self._notifier(client, box)
                )
                box.append(handle)
            client.handles.add(handle)
            return struct.pack("<I", handle)
        if cmd == DEL_NOTIFICATION:
            (handle,) = struct.unpack_from("<I", data)
            plc.unsubscribe(handle)
            client.handles.discard(handle)
            return b""
        raise ADSError(1793)

    def _read(self, group: int, offset: int, size: int) -> bytes:
        plc = self.plc
        if group == ADSIGRP_SYM_UPLOADINFO2:
            symbols, types = plc.symbol_table(), plc.type_table()
            info = struct.pack("<6I", len(plc.vars), len(symbols), len(plc.types), len(types), 0, 0)
            return info[:size]
        if group == ADSIGRP_SYM_UPLOAD:
            return plc.symbol_table()[:size]
        if group == ADSIGRP_SYM_DT_UPLOAD:
            return plc.type_table()[:size]
        return plc.read(group, offset, size)

    def _notifier(self, client: _Client, box: list[int]):
        """A PLC callback that pushes one DEVICENOTE frame to `client` for the handle in `box`."""

        def push(stamp_ns: int, data: bytes) -> None:
            filetime = stamp_ns // 100 + FILETIME_EPOCH
            sample = struct.pack("<II", box[0], len(data)) + data
            stamp = struct.pack("<QI", filetime, 1) + sample
            body = struct.pack("<II", len(stamp) + 4, 1) + stamp
            header = _AMS_HEADER.pack(
                client.net_id,
                client.port,
                self.net_id,
                DEMO_PORT,
                NOTIFICATION,
                STATE_REQUEST,
                len(body),
                0,
                0,
            )
            frame = struct.pack("<xxI", len(header) + len(body)) + header + body
            loop = self._loop
            if loop is not None and not loop.is_closed():
                loop.call_soon_threadsafe(self._send, client.writer, frame)

        return push

    @staticmethod
    def _send(writer: asyncio.StreamWriter, frame: bytes) -> None:
        if not writer.is_closing():
            writer.write(frame)


async def run_server(host: str = DEMO_HOST, port: int = ADS_TCP_PORT) -> None:
    """Serve until cancelled."""
    server = DemoServer(host=host, port=port)
    await server.start()
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await server.stop()
