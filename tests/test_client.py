"""The client end to end: in-process demo link for the logic, the demo PLC over
AMS/TCP through pyads for the real transport path, recovery and actions."""

from __future__ import annotations

import asyncio
import logging
import struct

import pytest
import zelos_sdk
from pyads.pyads_ex import ADSError

from tests.conftest import running, server, wait_until, wire, wire_client
from zelos_extension_ads import actions, ams, autoconfig
from zelos_extension_ads.cli import app
from zelos_extension_ads.client import SYM_VERSION, ADSClient, ADSRunner, SharedSource
from zelos_extension_ads.demo.plc import DEMO_NET_ID, DemoLink, DemoPlc
from zelos_extension_ads.demo.server import DemoServer
from zelos_extension_ads.symbols import HEALTH_EVENT, SymbolMap
from zelos_extension_ads.transport import ADSIGRP_SYM_VERSION, is_connection_error, needs_resync

_ = server  # fixture


def demo_client(plc: DemoPlc | None = None, **kwargs) -> ADSClient:
    plc = plc or DemoPlc()
    return ADSClient(
        host="localhost", name="demo", ams_net_id=DEMO_NET_ID, cycle_time=20,
        link_factory=lambda: DemoLink(plc), **kwargs
    )  # fmt: skip


async def test_notifications_trace_at_plc_time():
    """Changes arrive by notification, one row per POU and PLC timestamp; the
    health event reports the runtime; a static value is logged by the first sweep."""
    client = demo_client()
    async with running(client) as (_, rows):
        await wait_until(lambda: len(rows.of("MAIN", "nCycle")) >= 10, 10.0)
        assert client.status()["transport"] == "notification"
        assert client._trace_events["MAIN"].name == "demo/MAIN"
        cycles = rows.of("MAIN", "nCycle")
        assert all(t is not None for t, _ in cycles[1:])  # PLC-stamped after the first read
        assert [v for _, v in cycles] == sorted(v for _, v in cycles)
        assert rows.of("MAIN", "sState")[0][1] == "RUNNING"  # static: sweep, not notification
        assert rows.of("MAIN", "sState")[0][0] is not None  # on the notifications' time base
        assert rows.of("MAIN", "stMotor_fSpeed") and rows.of("GVL_IO", "nPressure")
        assert not rows.of("TwinCAT_SystemInfoVarList", "_AppInfo_AppName")
        health = rows.of(HEALTH_EVENT, "state_name")
        assert health and health[-1][1] == "RUN"


async def test_poll_transport_and_filters():
    client = demo_client(transport="poll", include=["GVL_IO.*"])
    async with running(client) as (_, rows):
        await wait_until(lambda: client._poll_count >= 5, 10.0)
        assert client.status()["polled"] == 2 and not rows.of("MAIN", "nCycle")
        assert all(t is None for t, _ in rows.of("GVL_IO", "fFlow"))


async def test_too_many_symbols_fall_back_to_polling(caplog):
    client = demo_client(max_notifications=3)
    async with running(client):
        await wait_until(lambda: client._connected, 10.0)
    assert client.status()["transport"] == "poll"
    assert "exceed max_notifications" in caplog.text


async def test_symbol_map_names_and_scales():
    """A scaled INT is traced as Float64: 1500 x 0.0123 must not land in an Int16."""
    symbol_map = SymbolMap.from_dict(
        {"events": {"motor": [{"symbol": "MAIN.stMotor.nRpm", "name": "rpm", "scale": 0.0123}]}}
    )
    client = demo_client(symbol_map=symbol_map)
    async with running(client, prefix="") as (_, rows):
        await wait_until(lambda: len(rows.of("motor", "rpm")) >= 2, 10.0)
    assert client._trace_events["motor"].name == "motor"
    assert client._declared["motor"] == [("rpm", zelos_sdk.DataType.Float64, "")]
    # The demo's rpm drifts from 1500 (1500 + 500 sin(t/5)): check the scale, not the instant.
    raw = rows.of("motor", "rpm")[0][1] / 0.0123
    assert raw == pytest.approx(round(raw)) and 1000 <= raw <= 2000
    assert client.status()["errors"] == 0


async def test_poll_resyncs_on_symbol_version_change(caplog):
    """Polled addresses go stale silently: a write after an online change that moved
    the variable still lands on it (by handle, resolved on the target); the health
    probe then sees 0xF008 moved and the client uploads again."""
    plc = DemoPlc()
    client = demo_client(plc, transport="poll")
    async with running(client):
        await wait_until(lambda: client._poll_count >= 2, 10.0)
        f = client.field("MAIN.bOut1")
        plc.online_change()  # bOut0 now sits at bOut1's old address
        assert await client.write_value(f, True) is True
        assert plc.vars["MAIN.bOut1"].data == b"\1" and plc.vars["MAIN.bOut0"].data == b"\0"
        assert not plc._handles
        await wait_until(lambda: "main.bout0" in client._uploaded, 10.0)
        assert client._watched[SYM_VERSION][0] == plc.read(ADSIGRP_SYM_VERSION, 0, 1)
        assert client.field("MAIN.bOut1").index_offset == f.index_offset + 1
        await client.write_value(client.field("MAIN.bOut1"), False)
    assert "symbol table changed; uploading the symbol table again" in caplog.text


async def test_online_change_counter_resyncs(caplog):
    """An online change leaves 0xF008 alone: OnlineChangeCnt, watched though TwinCAT_*
    is not traced, triggers the upload."""
    caplog.set_level(logging.INFO)
    plc = DemoPlc()
    client = demo_client(plc)
    async with running(client):
        await wait_until(lambda: client._connected, 10.0)
        assert len(client._watched) == 2 and "2 watches subscribed" in caplog.text
        with plc._lock:
            plc._poke("TwinCAT_SystemInfoVarList._AppInfo", 0, struct.pack("<I", 1))
        await wait_until(lambda: "online change; uploading" in caplog.text, 10.0)
        await wait_until(lambda: client._connected, 10.0)
    assert "Watching TwinCAT_SystemInfoVarList._AppInfo.OnlineChangeCnt at 0x4040:0x" in caplog.text
    assert caplog.text.count("online change; uploading") == 1


async def test_actions_read_write_state_and_selection():
    client = demo_client()
    async with running(client) as (runner, _):
        await wait_until(lambda: client._connected, 10.0)
        read = await asyncio.to_thread(actions.read_symbol, "main.ncycle")
        assert read["symbol"] == "MAIN.nCycle" and read["value"] > 0
        written = await asyncio.to_thread(actions.write_symbol, "MAIN.bOut1", "true")
        assert written["read_back"] is True
        assert (await asyncio.to_thread(actions.write_symbol, "MAIN.bOut1", "0"))[
            "read_back"
        ] is False
        state = await asyncio.to_thread(actions.plc_state)
        assert state["ads_state_name"] == "RUN"
        listed = await asyncio.to_thread(actions.list_symbols, "MAIN.stMotor.*")
        assert [s["field"] for s in listed["symbols"]] == [
            "stMotor_fSpeed",
            "stMotor_nRpm",
            "stMotor_bRunning",
        ]
        with pytest.raises(ValueError, match="Unknown symbol"):
            await asyncio.to_thread(actions.read_symbol, "MAIN.stMotor")
        with pytest.raises(ValueError, match="not a boolean"):
            await asyncio.to_thread(actions.write_symbol, "MAIN.bOut1", "maybe")
        assert actions.get_status()["count"] == 1
        two = ADSRunner([ADSClient(name="a"), ADSClient(name="b")])
        actions.set_runner(two)
        with pytest.raises(ValueError, match="set target to one of: a, b"):
            actions.read_symbol("MAIN.nCycle")
        actions.set_runner(runner)


def test_config_resolution_follows_advanced():
    config = {
        "targets": [
            {"host": "10.1.1.165", "transport": "poll", "include": ["MAIN.*"]},
            {"name": "cx", "host": "plc1", "ams_net_id": "5.1.2.3.1.1", "port": 852},
        ],
        "advanced": {"transport": "notification", "exclude": ["**.bDebug"], "timeout": 2.0},
    }
    targets = app.resolve_targets(config, app.resolve_advanced(config))
    assert [t["name"] for t in targets] == ["10_1_1_165", "cx"]
    assert (targets[0]["transport"], targets[0]["include"]) == ("poll", ["MAIN.*"])
    assert (targets[1]["transport"], targets[1]["exclude"]) == ("notification", ["**.bDebug"])
    assert targets[1]["port"] == 852 and targets[0]["timeout"] == 2.0
    clients = app.build_clients(targets)
    assert clients[1].ams_net_id == "5.1.2.3.1.1"
    with pytest.raises(SystemExit):
        app.resolve_targets({"targets": [{"host": "a"}, {"host": "a"}]}, app.resolve_advanced({}))
    with pytest.raises(SystemExit):
        app.resolve_targets({"servers": []}, app.resolve_advanced({}))


def test_error_classes():
    assert is_connection_error(ADSError(7)) and is_connection_error(ADSError(1861))
    assert is_connection_error(OSError("gone"))
    assert not is_connection_error(ADSError(1808)) and needs_resync(ADSError(1808))
    assert not is_connection_error(ADSError(1793)) and not needs_resync(ADSError(1793))


def test_add_route_reply():
    """Tag 1 is a u32 ADS result, read whole: 0x0700 is not success."""
    head = ams._UDP_MAGIC + bytes(4) + bytes((6, 0, 0, 0x80)) + bytes(8)
    for code in (0, 0x704, 0x700):
        reply = head + struct.pack("<I", 2) + struct.pack("<HH", 5, 1) + b"x"
        assert ams.parse_add_route_reply(reply + struct.pack("<HHI", 1, 4, code)) == code
    assert ams.parse_add_route_reply(head + struct.pack("<IHHH", 1, 1, 2, 0)) is None


def test_auto_config_resolves_a_host_name(monkeypatch):
    """A router answers from its IP: a DNS name in the form must still match it."""
    device = ams.Device(host="10.0.0.5", net_id="5.1.2.3.1.1", name="CX-1")
    monkeypatch.setattr(ams, "identify", lambda hosts, timeout: [device])
    monkeypatch.setattr(autoconfig.socket, "gethostbyname", lambda host: "10.0.0.5")
    result = autoconfig.check_targets([{"host": "plc.example"}])
    assert result["status"] == "success"
    assert result["config"]["targets"] == [{"host": "plc.example", "ams_net_id": "5.1.2.3.1.1"}]


# ─── Over the wire: pyads -> AMS/TCP -> demo PLC ────────────────────────────


@wire
async def test_wire_notifications_and_write(server: DemoServer):
    client = wire_client()
    async with running(client) as (_, rows):
        await wait_until(lambda: len(rows.of("MAIN", "fSine")) >= 5, 10.0)
        assert client.status()["subscribed"] == 9  # one per symbol-table entry, 14 leaves
        f = client.field("MAIN.bOut2")
        assert await client.write_value(f, True) is True
        assert struct.unpack("<?", server.plc.read(f.index_group, f.index_offset, 1))[0]
        await client.write_value(f, False)
        assert not server.plc._handles  # the write's symbol handle was released
        assert server.requests > 20
    # A clean close deleted every notification on the target.
    assert not server.plc._subs


@wire
async def test_wire_poll(server: DemoServer):
    client = wire_client(transport="poll")
    async with running(client) as (_, rows):
        await wait_until(lambda: len(rows.of("MAIN", "nCycle")) >= 5, 10.0)
    assert client.status()["polls"] >= 5 and client.status()["errors"] == 0


@wire
async def test_wire_recovers_after_server_restart(server: DemoServer, caplog):
    """A dead target is retried with backoff; it reconnects and traces again."""
    caplog.set_level(logging.INFO)
    client = wire_client()
    async with running(client) as (_, rows):
        await wait_until(lambda: len(rows.of("MAIN", "nCycle")) >= 3, 10.0)
        await server.stop()
        await wait_until(lambda: not client._connected, 10.0)
        assert client.status()["state"] == "disconnected"
        seen = len(rows.of("MAIN", "nCycle"))
        await server.start()
        await wait_until(
            lambda: client._connected and len(rows.of("MAIN", "nCycle")) > seen + 3, 20.0
        )
    assert "Connection to 127.0.0.1 lost" in caplog.text
    # Health marked the loss, then the recovery (the last row is the clean stop).
    connected = [v for _, v in rows.of(HEALTH_EVENT, "connected")]
    assert False in connected and True in connected[connected.index(False) :]


@wire
async def test_unreachable_at_start_stops_the_extension(caplog):
    """First contact is required: nothing on 48898 stops the runner with one ERROR."""
    client = wire_client()
    client.start(SharedSource(__import__("zelos_sdk").TraceSource("ADS")))
    runner = ADSRunner([client])
    await asyncio.wait_for(runner._run_async(), 15.0)
    assert runner.failed_at_start == [client]
    assert "Target 'demo' (127.0.0.1): cannot connect: no TCP connection to 127.0.0.1:48898" in (
        caplog.text
    )
