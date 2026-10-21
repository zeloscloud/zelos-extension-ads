"""Find TwinCAT targets for the config form's Auto-configure button: ask the
form's targets for their NetIds, or with none, look on this machine and its
network.

Read-only and session-less: Beckhoff's UDP 48899 identify, which every TwinCAT
router answers without a route. No TCP, no subnet sweep. Runtime ports cannot be
listed without a route, so a found device becomes one target on AMS port 851.
"""

from __future__ import annotations

import logging
from typing import Any

import zelos_sdk

from zelos_extension_ads import ams

logger = logging.getLogger(__name__)

IDENTIFY_SECONDS = 2.0


def _entry(device: ams.Device, taken: set[str]) -> dict[str, Any]:
    label = device.name or device.host
    name = base = zelos_sdk.sanitize_name(label, kind="source")
    k = 2
    while name in taken:
        name, k = f"{base}_{k}", k + 1
    taken.add(name)
    return {"name": name, "host": device.host, "ams_net_id": device.net_id}


def _describe(device: ams.Device) -> str:
    version = f", TwinCAT {device.version}" if device.version else ""
    return f"{device.name or 'TwinCAT'} at {device.host} ({device.net_id}{version})"


def check_targets(targets: list[dict[str, Any]]) -> dict[str, Any]:
    """The form's targets, each kept as entered with its NetId filled in from the
    router at its host; one that does not answer is named."""
    hosts = [str(t.get("host") or "localhost").strip() or "localhost" for t in targets]
    devices = {d.host: d for d in ams.identify(sorted(set(hosts)), IDENTIFY_SECONDS)}
    by_name = {d.name.lower(): d for d in devices.values() if d.name}
    out, found, silent = [], [], []
    for entry, host in zip(targets, hosts, strict=True):
        entry = dict(entry)
        out.append(entry)
        device = _resolve(host, devices, by_name)
        if device is None:
            silent.append(host)
            continue
        if not entry.get("ams_net_id"):
            entry["ams_net_id"] = device.net_id
        elif entry["ams_net_id"] != device.net_id:
            silent.append(f"{host} (answers as {device.net_id}, form says {entry['ams_net_id']})")
            continue
        found.append(_describe(device))
    notes = []
    if found:
        notes.append(f"Found {'; '.join(found)}.")
    if silent:
        notes.append(
            f"No TwinCAT router answered at {', '.join(silent)} (UDP 48899): check the host, "
            "or set its AMS NetId by hand."
        )
    if not found:
        return {"status": "error", "message": " ".join(notes)}
    return {"status": "success", "message": " ".join(notes), "config": {"targets": out}}


def _resolve(
    host: str, by_host: dict[str, ams.Device], by_name: dict[str, ams.Device]
) -> ams.Device | None:
    """The device that answered for `host`: by the address it answered from, else by
    its host name (a name in the form answers from its IP)."""
    if host in by_host:
        return by_host[host]
    if host in ("localhost", "127.0.0.1"):
        return by_host.get("127.0.0.1") or by_host.get("localhost")
    return by_name.get(host.lower())


def probe() -> dict[str, Any]:
    """TwinCAT routers on this machine and its broadcast domain, one target each."""
    devices = ams.identify(["127.0.0.1"], IDENTIFY_SECONDS, broadcast=True)
    if not devices:
        return {
            "status": "error",
            "message": "No TwinCAT router answered on this machine or its network (UDP 48899 "
            "broadcast). Add a target with its host and AMS NetId.",
        }
    taken: set[str] = set()
    targets = [_entry(d, taken) for d in devices]
    found = "; ".join(_describe(d) for d in devices)
    message = f"Found {found}. Each is set to AMS port 851 (the first PLC)."
    if not ams.native_router():
        message += (
            " A target must hold a route for this machine before the first start: "
            "add_route action, or TwinCAT XAE."
        )
    return {"status": "success", "message": message, "config": {"targets": targets}}
