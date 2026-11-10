"""Against a real TwinCAT target; skipped unless ZELOS_ADS_HOST is set.

    ZELOS_ADS_HOST=<ip> [ZELOS_ADS_NET_ID=<netid>] [ZELOS_ADS_PORT=851] just hil

The target must hold a route for this machine (add_route action) and run a
program with MAIN.nCycle (UDINT, +1 per cycle), MAIN.fSine (LREAL) and
MAIN.bOut1 / MAIN.bOut2 (BOOL). Only bOut1 is written, and restored to FALSE.
PLC state is read, never changed. The EtherCAT test expects the master in OP
with at least one slave; it reads only.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess

import pytest
import zelos_sdk

from tests.conftest import running, wait_until
from zelos_extension_ads import actions
from zelos_extension_ads.ams import ADS_TCP_PORT
from zelos_extension_ads.cli.app import set_local_router
from zelos_extension_ads.client import ADSClient, ADSRunner, SharedSource
from zelos_extension_ads.ethercat import MASTER_EVENT
from zelos_extension_ads.symbols import HEALTH_EVENT

HOST = os.environ.get("ZELOS_ADS_HOST", "")
pytestmark = pytest.mark.skipif(not HOST, reason="ZELOS_ADS_HOST not set")


def bench_client(**kwargs) -> ADSClient:
    return ADSClient(
        host=HOST,
        name="bench",
        ams_net_id=os.environ.get("ZELOS_ADS_NET_ID", ""),
        port=int(os.environ.get("ZELOS_ADS_PORT", "851")),
        include=["MAIN.*"],
        **kwargs,
    )


async def test_bench_notifications_and_write():
    client = bench_client()
    async with running(client) as (_, rows):
        await wait_until(lambda: len(rows.of("MAIN", "nCycle")) >= 10, 20.0)
        cycles = [v for _, v in rows.of("MAIN", "nCycle")]
        assert cycles == sorted(cycles) and cycles[-1] > cycles[0]
        assert rows.of("MAIN", "fSine")
        status = client.status()
        assert status["plc_state"] == "RUN" and status["transport"] == "notification"
        assert abs(status["clock_skew_ms"]) < 5000
        try:
            assert (await asyncio.to_thread(actions.write_symbol, "MAIN.bOut1", "true"))[
                "read_back"
            ] is True
            await wait_until(lambda: any(v is True for _, v in rows.of("MAIN", "bOut1")), 5.0)
        finally:
            assert (await asyncio.to_thread(actions.write_symbol, "MAIN.bOut1", "false"))[
                "read_back"
            ] is False
        state = await asyncio.to_thread(actions.plc_state)
        assert state["ads_state_name"] == "RUN"
        assert rows.of(HEALTH_EVENT, "state_name")[-1][1] == "RUN"


async def test_bench_poll():
    client = bench_client(transport="poll", cycle_time=50)
    async with running(client) as (_, rows):
        await wait_until(lambda: len(rows.of("MAIN", "nCycle")) >= 10, 20.0)
    assert client.status()["errors"] == 0


async def test_bench_ethercat():
    """The master (discovered on the I/O server) is in OP and lists its slaves; a CoE read
    answers or names the missing mailbox."""
    client = bench_client(ethercat=True, ethercat_interval=0.5)
    async with running(client) as (_, rows):
        await wait_until(lambda: len(rows.of(MASTER_EVENT, "al_state_name")) >= 2, 20.0)
        assert rows.of(MASTER_EVENT, "al_state_name")[-1][1] == "OP"
        assert (
            rows.of(MASTER_EVENT, "cyclic_frames")[-1][1]
            > rows.of(MASTER_EVENT, "cyclic_frames")[0][1]
        )
        listed = await asyncio.to_thread(actions.ethercat_slaves)
        assert listed["count"] >= 1 and listed["al_state_name"] == "OP"
        assert all(s["al_state_name"] == "OP" and not s["lost"] for s in listed["slaves"])
        first = listed["slaves"][0]
        assert rows.of(f"{MASTER_EVENT}/{first['slave']}", "al_state_name")[-1][1] == "OP"
        try:
            sdo = await asyncio.to_thread(actions.ethercat_read_sdo, first["slave"], "0x1018", 1)
            assert sdo["value"] == first["vendor_id"]
        except ValueError as e:
            assert "no CoE mailbox" in str(e)
    assert client.status()["errors"] == 0 and client.status()["ethercat_slaves"] >= 1


def drop_ads_links() -> int:
    """Shut down this process's AMS/TCP sockets, as a cable pull would end them. Found by
    lsof: duplicating an arbitrary descriptor kills the process on macOS (guarded fds)."""
    listing = subprocess.run(
        ["lsof", "-a", "-n", "-P", "-p", str(os.getpid()), f"-iTCP:{ADS_TCP_PORT}", "-Ff"],
        capture_output=True,
        text=True,
    ).stdout
    fds = [int(line[1:]) for line in listing.splitlines() if line[:1] == "f"]
    for fd in fds:
        with socket.socket(fileno=os.dup(fd)) as s:
            s.shutdown(socket.SHUT_RDWR)
    return len(fds)


async def test_bench_reconnects_after_link_loss():
    """Two NetIds (runtime + EtherCAT master) share one connection: every drop must let
    the next connect open a fresh one, not reuse the dead socket."""
    client = bench_client(ethercat=True)
    async with running(client) as (_, rows):
        await wait_until(lambda: client.status()["state"] == "ok", 20.0)
        for _ in range(5):
            errors = client.status()["errors"]
            assert drop_ads_links() >= 1
            # A reconnect can take 35 ms: the counted loss, not a state seen between polls.
            await wait_until(lambda n=errors: client.status()["errors"] > n, 5.0)
            await wait_until(lambda: client.status()["state"] == "ok", 20.0)
            seen = len(rows.of("MAIN", "nCycle"))
            await wait_until(lambda n=seen: len(rows.of("MAIN", "nCycle")) > n + 3, 5.0)


async def test_bench_one_connection_per_host():
    """TwinCAT drops a connection when the same IP opens another: a second target on the
    host, and an auto-configure probe of it, must ride the live one."""
    first = bench_client()
    second = ADSClient(host=HOST, name="bench2", port=first.port, include=["MAIN.nCycle"])
    set_local_router([first, second])
    shared = SharedSource(zelos_sdk.TraceSource("ADS"))
    first.start(shared)
    second.start(shared)
    runner = ADSRunner([first, second])
    task = asyncio.create_task(runner._run_async())
    try:
        both = [first, second]
        await wait_until(lambda: all(c.status()["state"] == "ok" for c in both), 20.0)
        # TwinCAT 4026 dropped a connection some seconds old on every other probe.
        await asyncio.sleep(10.0)
        errors = [c.status()["errors"] for c in both]
        for _ in range(4):
            await asyncio.to_thread(actions.auto_config, {"targets": [{"name": "x", "host": HOST}]})
            await asyncio.sleep(5.0)
        assert [c.status()["errors"] for c in both] == errors
        assert all(c.status()["state"] == "ok" for c in both)
    finally:
        runner.stop()
        await asyncio.wait_for(task, 10.0)
