"""Against a real TwinCAT target; skipped unless ZELOS_ADS_HOST is set.

    ZELOS_ADS_HOST=<ip> [ZELOS_ADS_NET_ID=<netid>] [ZELOS_ADS_PORT=851] just hil

The target must hold a route for this machine (add_route action) and run a
program with MAIN.nCycle (UDINT, +1 per cycle), MAIN.fSine (LREAL) and
MAIN.bOut1 / MAIN.bOut2 (BOOL). Only bOut1 is written, and restored to FALSE.
PLC state is read, never changed.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from tests.conftest import running, wait_until
from zelos_extension_ads import actions
from zelos_extension_ads.client import ADSClient
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
