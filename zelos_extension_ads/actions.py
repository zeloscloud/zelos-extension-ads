"""ADS actions, free functions registered under `<ACTION_PREFIX>/<name>`.

Free functions, not client methods: one surface whether or not a client runs.
`target` may be omitted with one target. Failures raise: the protocol reads its
verdict from an exception, so {"success": False} would report a successful run.
"""

from __future__ import annotations

import inspect
import logging
import socket
import sys
import time
from typing import TYPE_CHECKING, Any

import zelos_sdk

from zelos_extension_ads import ams
from zelos_extension_ads import ethercat as ecat
from zelos_extension_ads import symbols as sym
from zelos_extension_ads.symbols import sanitize
from zelos_extension_ads.transport import describe_error

if TYPE_CHECKING:
    from zelos_sdk.actions import ActionsRegistry

    from zelos_extension_ads.client import ADSClient, ADSRunner

logger = logging.getLogger(__name__)

_runner: ADSRunner | None = None


def set_runner(runner: ADSRunner | None) -> None:
    """Bind the runner whose clients every action in this module operates on."""
    global _runner
    _runner = runner


def _get_runner() -> ADSRunner:
    if _runner is None:
        raise RuntimeError("No ADS target is configured on this extension")
    return _runner


def _get_client(target: str = "") -> ADSClient:
    """The named target's client; the only one when `target` is empty."""
    clients = _get_runner().clients
    names = ", ".join(clients)
    if not target:
        if len(clients) == 1:
            return next(iter(clients.values()))
        raise ValueError(f"Several targets are configured; set target to one of: {names}")
    # Sanitized like the configured names, so `192.168.1.10` finds `192_168_1_10`.
    client = clients.get(sanitize(target.strip(), "source"))
    if client is None:
        raise ValueError(f"Unknown target '{target}'. Targets: {names}")
    return client


def _run(client: ADSClient, coro: Any, what: str, started: float | None = None) -> Any:
    """Dispatch an action coroutine; anything not self-describing becomes a RuntimeError.

    `started` marks a write: on a timeout the request is still queued behind the
    worker's current call, or sent and unanswered, so it may yet be applied.
    """
    try:
        return _get_runner()._run_coro(coro, client.timeout + 1.0)
    except Exception as e:
        if started is not None and isinstance(e, TimeoutError):
            raise TimeoutError(
                f"{what}: no response within {time.monotonic() - started:.1f}s; the target "
                "may still apply it, read the symbol to confirm"
            ) from e
        if isinstance(e, ValueError | RuntimeError | TimeoutError | OSError):
            raise
        logger.exception("%s failed", what)
        raise RuntimeError(f"{what} failed: {describe_error(e)}") from e


def _target_field(func: Any) -> Any:
    return zelos_sdk.action.text(
        "target",
        title="Target",
        required=False,
        default="",
        description="Target name; may be omitted when only one is configured",
    )(func)


# ─── Status and symbols ─────────────────────────────────────────────────────


@zelos_sdk.action(
    "Get Status",
    "Get connection and transport status; every target when omitted",
    read_only=True,
)
@_target_field
def get_status(target: str = "") -> dict[str, Any]:
    if target:
        return _get_client(target).status()
    statuses = [c.status() for c in _get_runner().clients.values()]
    return {"targets": statuses, "count": len(statuses)}


@zelos_sdk.action(
    "List Symbols",
    "The scalar symbols the target holds (structs and arrays flattened) and where each "
    "is traced; every target's when omitted",
    read_only=True,
)
@_target_field
@zelos_sdk.action.text(
    "pattern",
    title="Pattern",
    required=False,
    default="",
    description="Symbol glob, e.g. MAIN.* or **.bRun; empty lists all",
)
def list_symbols(pattern: str = "", target: str = "") -> dict[str, Any]:
    clients = [_get_client(target)] if target else list(_get_runner().clients.values())
    symbols = [{"target": c.name, **s} for c in clients for s in c.symbols(pattern.strip())]
    return {"symbols": symbols, "count": len(symbols)}


@zelos_sdk.action(
    "PLC State", "The runtime's ADS state (RUN, STOP, CONFIG...) and version", read_only=True
)
@_target_field
def plc_state(target: str = "") -> dict[str, Any]:
    client = _get_client(target)
    return _run(client, client.read_state(), "PLC state")


# ─── Read / write ───────────────────────────────────────────────────────────


@zelos_sdk.action("Read Symbol", "Read one scalar symbol by name", read_only=True)
@_target_field
@zelos_sdk.action.text(
    "symbol", title="Symbol", description="e.g. MAIN.nCycle or MAIN.stMotor.fSpeed"
)
def read_symbol(symbol: str, target: str = "") -> dict[str, Any]:
    client = _get_client(target)
    f = client.field(symbol)
    value = _run(client, client.read_value(f), f"Read of '{f.symbol}'")
    return {"symbol": f.symbol, "type": f.type_name, "value": value}


@zelos_sdk.action(
    "Write Symbol",
    "Write a value to one scalar symbol by name and read it back. The extension never "
    "writes on its own; a symbol the PLC program overwrites each cycle reads back as "
    "the program left it.",
)
@_target_field
@zelos_sdk.action.text("symbol", title="Symbol", description="e.g. MAIN.bOut1")
@zelos_sdk.action.text(
    "value", title="Value", description="Coerced to the symbol's type; true/false for BOOL"
)
def write_symbol(symbol: str, value: str, target: str = "") -> dict[str, Any]:
    client = _get_client(target)
    f = client.field(symbol)
    typed = sym.coerce_text(str(value), f.data_type)
    started = time.monotonic()
    read_back = _run(client, client.write_value(f, typed), f"Write to '{f.symbol}'", started)
    return {"symbol": f.symbol, "type": f.type_name, "value": typed, "read_back": read_back}


# ─── EtherCAT (the target's EtherCAT master, when enabled on it) ───────────


@zelos_sdk.action(
    "EtherCAT Slaves",
    "The TwinCAT EtherCAT master's state and frame counters, and every slave's state, "
    "link status, identity and CRC counters, read now. Needs EtherCAT diagnostics "
    "enabled on the target.",
    read_only=True,
)
@_target_field
def ethercat_slaves(target: str = "") -> dict[str, Any]:
    client = _get_client(target)
    snapshot = _run(client, client.read_ethercat(), "EtherCAT read")
    return {
        "target": client.name,
        "ams_net_id": client.ethercat_net_id,
        **snapshot.as_dict(),
        "count": len(snapshot.slaves),
    }


@zelos_sdk.action(
    "EtherCAT Read SDO",
    "Read a CoE object (SDO upload) from an EtherCAT slave through the TwinCAT master. "
    "A slave without a mailbox (couplers, digital terminals) reports so.",
    read_only=True,
)
@_target_field
@zelos_sdk.action.text(
    "slave",
    title="Slave",
    description="Position (4), trace name (4_EL3004) or station address (1004); "
    "see ethercat_slaves",
)
@zelos_sdk.action.text("index", title="Index", description="e.g. 0x1018")
@zelos_sdk.action.integer(
    "subindex", title="Subindex", required=False, default=0, minimum=0, maximum=255
)
@zelos_sdk.action.select(
    "data_type",
    title="Data Type",
    choices=list(ecat.SDO_TYPES),
    required=False,
    default="auto",
    description="How to decode the bytes; auto picks an unsigned integer by length",
)
def ethercat_read_sdo(
    slave: str, index: str, subindex: int = 0, data_type: str = "auto", target: str = ""
) -> dict[str, Any]:
    client = _get_client(target)
    idx = ecat.parse_index(index)
    if data_type not in ecat.SDO_TYPES:
        raise ValueError(f"data_type must be one of {', '.join(ecat.SDO_TYPES)}")
    found, data = _run(
        client, client.read_sdo(slave, idx, int(subindex)), f"SDO upload 0x{idx:04x}:{subindex}"
    )
    used, value = ecat.decode_sdo(data, data_type)
    return {
        "slave": found.trace_name,
        "address": found.address,
        "index": f"0x{idx:04x}",
        "subindex": int(subindex),
        "data_type": used,
        "bytes": data.hex(),
        "value": value,
    }


# ─── Routes (standalone: the target's router, no session) ──────────────────


@zelos_sdk.action(
    "Add Route",
    "Add a route for this machine to a TwinCAT target, through Beckhoff's UDP handshake "
    "(the same as XAE's Add Route). The user name and password go to the target once "
    "and the extension stores neither. Needed once per target from a machine without a TwinCAT "
    "router (Linux, macOS); a TwinCAT host adds routes with XAE or adstool instead. "
    "The password crosses the network recoverable (Beckhoff advisory 2017-002): run it "
    "on a trusted network, or add the route on the target.",
    standalone=True,
)
@zelos_sdk.action.text("host", title="Target Host", description="IP address of the TwinCAT target")
@zelos_sdk.action.text(
    "username",
    title="User Name",
    description="A Windows / TwinCAT user on the target, e.g. Administrator",
)
@zelos_sdk.action.text(
    "password",
    title="Password",
    description="That user's password; sent once, not stored by the extension. Turn off the action panel's Persist Parameters to keep it out of the layout",
    widget="password",
)
@zelos_sdk.action.text(
    "local_ams_net_id",
    title="Local AMS NetId",
    required=False,
    default="",
    description="The NetId to route to this machine; empty uses the one the running "
    "extension presents, else derives it as it would (Advanced > Local AMS NetId, else "
    "this machine's IP toward the target + .1.1)",
)
@zelos_sdk.action.text(
    "route_name",
    title="Route Name",
    required=False,
    default="",
    description="Name of the route on the target; empty uses this machine's host name",
)
def add_route(
    host: str, username: str, password: str, local_ams_net_id: str = "", route_name: str = ""
) -> dict[str, Any]:
    host = host.strip()
    if not host:
        raise ValueError("host is required")
    if not username:
        raise ValueError("username is required")
    if ams.native_router():
        raise ValueError(
            "This machine runs a TwinCAT router: add the route in it with XAE or "
            "`adstool <host> addroute`; this action is for machines without one"
        )
    local_host = ams.outbound_ip(host)
    # Running, the process presents one NetId: that is the one to route, whatever
    # interface `host` sits on; stopped, derive it as the next start will.
    net_id = ams.parse_net_id(
        local_ams_net_id or ams.local_net_id() or _advanced_local_net_id() or local_host + ".1.1"
    )
    name = route_name.strip() or socket.gethostname()
    try:
        added = ams.add_route(host, username, password, net_id, local_host, name)
    except TimeoutError as e:
        raise TimeoutError(f"no answer from {host} on UDP 48899") from e
    if not added:
        raise ValueError(f"{host} refused the credentials for user '{username}'")
    return {
        "message": f"Route '{name}' for {net_id} at {local_host} added on {host}",
        "host": host,
        "local_ams_net_id": net_id,
        "local_host": local_host,
        "route_name": name,
    }


def _advanced_local_net_id() -> str:
    return str((_saved_config().get("advanced") or {}).get("local_ams_net_id") or "")


# ─── Config-form hook (standalone: runs with the extension stopped) ─────────


@zelos_sdk.action(
    "Auto-configure",
    "Ask each target in the form for its AMS NetId (unsaved edits included; older apps: "
    "the saved config) and fill it in. With none, find TwinCAT routers on this machine "
    "and its network (UDP broadcast) and add one target each. Review, then save and start.",
    standalone=True,
    hidden=True,
)
@zelos_sdk.action.object(
    "config",
    properties={},
    title="Config",
    description="The config form's current (possibly unsaved) data. Empty: the saved config",
    required=False,
)
def auto_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """`config` keys replace the form's: only `targets`, so Advanced survives."""
    from zelos_extension_ads.autoconfig import check_targets, probe

    if config is None:
        config = _saved_config()
    targets = [t for t in config.get("targets") or [] if isinstance(t, dict)]
    return check_targets(targets) if targets else probe()


def _saved_config() -> dict[str, Any]:
    """The saved config, or {} when there is none."""
    try:
        from zelos_sdk.extensions import load_config

        return load_config() or {}
    except Exception:  # no config yet, or it does not validate
        return {}


# ─── Registration helper ────────────────────────────────────────────────────


def register_actions(registry: ActionsRegistry) -> list[str]:
    """Register every @action function here by bare name; `init(name=ACTION_PREFIX)`
    adds the prefix. Returns the registered names."""
    module = sys.modules[__name__]
    registered: list[str] = []
    for name, obj in inspect.getmembers(module):
        if name.startswith("_"):
            continue
        if inspect.isfunction(obj) and hasattr(obj, "_action"):
            registry.register(obj, name=name)
            registered.append(name)
    logger.info("Registered %d ADS actions", len(registered))
    return registered
