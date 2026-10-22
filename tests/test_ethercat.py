"""EtherCAT diagnostics through the demo master: the trace, and the two actions."""

from __future__ import annotations

import asyncio

import pytest

from tests.conftest import running, wait_until
from zelos_extension_ads import actions
from zelos_extension_ads.client import ADSClient
from zelos_extension_ads.demo.plc import DEMO_ECAT_NET_ID, DEMO_NET_ID, DemoLink, DemoPlc
from zelos_extension_ads.ethercat import MASTER_EVENT, MASTER_PORT, al_state_name, decode_sdo


async def test_ethercat_trace_and_actions():
    plc = DemoPlc()
    client = ADSClient(
        host="localhost", name="demo", ams_net_id=DEMO_NET_ID, cycle_time=20, transport="poll",
        include=["MAIN.nCycle"], link_factory=lambda: DemoLink(plc), ethercat=True,
        ethercat_interval=0.1, ethercat_link_factory=lambda: DemoLink(plc, MASTER_PORT),
    )  # fmt: skip
    async with running(client) as (_, rows):
        await wait_until(lambda: len(rows.of(MASTER_EVENT, "cyclic_frames")) >= 3, 10.0)
        assert client.ethercat_net_id == DEMO_ECAT_NET_ID
        assert rows.of(MASTER_EVENT, "al_state_name")[-1][1] == "OP"
        assert rows.of(MASTER_EVENT, "slaves")[-1][1] == 4
        assert client._trace_events["_ethercat/4_EL3004"].name == "demo/_ethercat/4_EL3004"
        # A slave is logged on change only: one row so far, then the drop.
        assert len(rows.of("_ethercat/3_EL2004", "al_state")) == 1
        plc.ethercat.drop(1003)
        await wait_until(lambda: rows.of("_ethercat/3_EL2004", "lost")[-1][1] is True, 5.0)
        assert rows.of("_ethercat/3_EL2004", "al_state_name")[-1][1] == "NONE"
        assert rows.of("_ethercat/3_EL2004", "link_state_name")[-1][1] == "not present"
        assert len(rows.of("_ethercat/1_EK1100", "al_state")) == 1

        listed = await asyncio.to_thread(actions.ethercat_slaves)
        assert listed["count"] == 4 and listed["al_state_name"] == "OP"
        assert [s["name"] for s in listed["slaves"]] == ["EK1100", "EL1004", "EL2004", "EL3004"]
        assert listed["slaves"][2]["lost"] is True and listed["slaves"][3]["address"] == 1004
        sdo = await asyncio.to_thread(actions.ethercat_read_sdo, "4_EL3004", "0x1018", 2)
        assert (sdo["value"], sdo["data_type"], sdo["address"]) == (0x0BBC3052, "u32", 1004)
        name = await asyncio.to_thread(actions.ethercat_read_sdo, "1004", "0x1008", 0, "string")
        assert name["value"] == "EL3004" and name["slave"] == "4_EL3004"
        with pytest.raises(ValueError, match="no CoE mailbox"):
            await asyncio.to_thread(actions.ethercat_read_sdo, "1", "0x1018", 1)
        with pytest.raises(ValueError, match="No EtherCAT slave '9'"):
            await asyncio.to_thread(actions.ethercat_read_sdo, "9", "0x1018", 1)
    assert client.status()["ethercat_slaves"] == 4
    with pytest.raises(ValueError, match="EtherCAT diagnostics are off"):
        ADSClient(name="off")._require_ethercat()
    assert decode_sdo(b"\x01\x02\x03", "u16") == ("hex", "010203")
    assert al_state_name(0x34) == "SAFEOP+ERROR+INVALID_VPRS"
