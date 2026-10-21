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

import pytest

from tests.conftest import running, wait_until
from zelos_extension_ads import actions
from zelos_extension_ads.client import ADSClient
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
    """The master (target NetId with .2.1) is in OP and lists its slaves; a CoE read
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
