"""Shared helpers: the demo PLC behind AMS/TCP, clients through the app's resolution."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
import zelos_sdk

from zelos_extension_ads import actions
from zelos_extension_ads.ams import ADS_TCP_PORT
from zelos_extension_ads.client import ADSClient, ADSRunner, SharedSource
from zelos_extension_ads.demo.plc import DEMO_NET_ID, DemoPlc
from zelos_extension_ads.demo.server import DemoServer


def port_free(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) != 0


#: AdsLib connects to 48898 only; a TwinCAT or another demo there skips the wire tests.
wire = pytest.mark.skipif(not port_free(ADS_TCP_PORT), reason="127.0.0.1:48898 is in use")


@pytest.fixture
async def server() -> AsyncIterator[DemoServer]:
    """The demo PLC on 127.0.0.1:48898, in the test's loop."""
    srv = DemoServer(DemoPlc())
    await srv.start()
    try:
        yield srv
    finally:
        await srv.stop()


class Rows:
    """Every row a client logs after this wraps it: (event, time_ns or None, fields)."""

    def __init__(self, client: ADSClient) -> None:
        self.rows: list[tuple[str, int | None, dict[str, Any]]] = []
        declare = client._declare

        def wrapped(name: str, fields: Any) -> None:
            declare(name, fields)
            client._trace_events[name] = self._wrap(name, client._trace_events[name])

        client._declare = wrapped  # type: ignore[method-assign]
        for name, event in list(client._trace_events.items()):
            client._trace_events[name] = self._wrap(name, event)

    def _wrap(self, name: str, event: Any) -> Any:
        rows = self.rows

        class Event:
            name = event.name

            def log(self, **fields: Any) -> None:
                rows.append((name, None, fields))
                event.log(**fields)

            def log_at(self, stamp: int, **fields: Any) -> None:
                rows.append((name, stamp, fields))
                event.log_at(stamp, **fields)

        return Event()

    def of(self, event: str, field: str) -> list[tuple[int | None, Any]]:
        return [(t, f[field]) for e, t, f in self.rows if e == event and field in f]


def wire_client(**kwargs: Any) -> ADSClient:
    """A client to the demo PLC over AMS/TCP through pyads."""
    return ADSClient(
        host="127.0.0.1", name="demo", ams_net_id=DEMO_NET_ID, cycle_time=20, timeout=2.0, **kwargs
    )


@contextlib.asynccontextmanager
async def running(client: ADSClient, prefix: str = "ADS") -> AsyncIterator[tuple[ADSRunner, Rows]]:
    """`client` started on the prefix's layout and run in a runner; stopped on exit."""
    client.start(SharedSource(zelos_sdk.TraceSource(prefix)) if prefix else None)
    rows = Rows(client)
    runner = ADSRunner([client])
    actions.set_runner(runner)
    task = asyncio.create_task(runner._run_async())
    try:
        yield runner, rows
    finally:
        actions.set_runner(None)
        runner.stop()
        await asyncio.wait_for(task, 10.0)


async def wait_until(predicate: Any, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"Condition not met within {timeout}s")
        await asyncio.sleep(0.05)
