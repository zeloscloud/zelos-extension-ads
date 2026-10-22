"""App mode: run from the Zelos App's saved config, or demo mode.

Startup problems are one logger.error line plus exit 1; tracebacks are for bugs.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any

import zelos_sdk
from zelos_sdk.extensions import ConfigValidationError, load_config
from zelos_sdk.hooks.logging import TraceLoggingHandler

from zelos_extension_ads import ACTION_PREFIX, ams
from zelos_extension_ads import actions as ads_actions
from zelos_extension_ads.client import (
    LOG_EVENT,
    LOG_SOURCE_NAME,
    ADSClient,
    ADSRunner,
    SharedSource,
    default_target_name,
    install_log_handler,
)
from zelos_extension_ads.symbols import SymbolMap, sanitize
from zelos_extension_ads.transport import describe_error

logger = logging.getLogger(__name__)

DEMO_NAME = "demo"
# A forced exit flushes the traces this long, then leaves; the bound on shutdown is ~3 s.
FORCED_FLUSH_TIMEOUT = 0.3

#: Defaults of the schema's `advanced` object: settings shared by every target.
ADVANCED_DEFAULTS: dict[str, Any] = {
    "prefix": "ADS",
    "timeout": 5.0,
    "log_level": "INFO",
    "local_ams_net_id": "",
    "transport": "notification",
    "max_notifications": 500,
    "min_update_interval": 60.0,
    "ethercat_interval": 1.0,
    "include": [],
    "exclude": [],
}

#: Defaults of one `targets[]` entry. "default" / [] inherit from `advanced`.
TARGET_DEFAULTS: dict[str, Any] = {
    "name": "",
    "host": "localhost",
    "ams_net_id": "",
    "port": 851,
    "symbol_map_file": "",
    "cycle_time": 100,
    "transport": "default",
    "include": [],
    "exclude": [],
    "ethercat": False,
    "ethercat_ams_net_id": "",
}

TOP_LEVEL_KEYS = frozenset({"targets", "advanced"})

#: Shared client settings every ADSClient takes from `advanced`.
_SHARED = ("timeout", "max_notifications", "min_update_interval", "ethercat_interval")


def resolve_advanced(config: dict[str, Any]) -> dict[str, Any]:
    """`advanced` over its defaults; an absent `prefix` is the default, an empty one clears it."""
    return {**ADVANCED_DEFAULTS, **(config.get("advanced") or {})}


def read_raw_config() -> dict[str, Any]:
    """config.json as saved."""
    path = Path(os.environ.get("ZELOS_CONFIG_PATH") or "config.json")
    return json.loads(path.read_text()) if path.is_file() else {}


def reject_unknown_shape(config: dict[str, Any]) -> None:
    """Exit on a config of another shape, rather than misread it."""
    unknown = sorted(k for k in config if k not in TOP_LEVEL_KEYS)
    if unknown:
        logger.error(
            "Config holds unknown top-level keys %s: targets go under targets[] with shared "
            "settings under advanced; reconfigure the extension",
            ", ".join(unknown),
        )
        sys.exit(1)


def trace_name(value: str) -> str:
    """A prefix or target name as the trace catalog addresses it."""
    value = value.strip()
    return sanitize(value, "source") if value else ""


def _inherit(target: dict[str, Any], advanced: dict[str, Any]) -> dict[str, Any]:
    """A target's effective settings: its own, else ("default" / [] / None) the advanced one."""
    return {
        k: advanced[k] if target[k] in ("", "default", None, []) else target[k]
        for k in ("transport", "include", "exclude")
    }


def resolve_targets(config: dict[str, Any], advanced: dict[str, Any]) -> list[dict[str, Any]]:
    """ADSClient arguments per configured target, or exit."""
    reject_unknown_shape(config)
    targets = config.get("targets") or []
    if not targets:
        logger.error("No targets configured: add one under Targets")
        sys.exit(1)

    resolved: list[dict[str, Any]] = []
    seen: dict[str, str] = {}  # name -> host
    for i, entry in enumerate(targets):
        target = {**TARGET_DEFAULTS, **entry}
        host = str(target["host"]).strip()
        if not host:
            logger.error("targets[%d] has no host", i)
            sys.exit(1)
        name = trace_name(str(target["name"])) or default_target_name(host)
        if name in (LOG_EVENT, LOG_SOURCE_NAME):
            logger.error("Target %s: name '%s' is reserved for the extension's log", host, name)
            sys.exit(1)
        if name in seen:
            logger.error(
                "Targets %s and %s both resolve to name '%s'; set a distinct name on one",
                seen[name],
                host,
                name,
            )
            sys.exit(1)
        seen[name] = host
        resolved.append(
            {
                "name": name,
                "host": host,
                "ams_net_id": str(target["ams_net_id"]).strip(),
                "port": int(target["port"]),
                "symbol_map_file": target["symbol_map_file"],
                "cycle_time": int(target["cycle_time"]),
                "ethercat": bool(target["ethercat"]),
                "ethercat_ams_net_id": str(target["ethercat_ams_net_id"]).strip(),
                **_inherit(target, advanced),
                **{k: advanced[k] for k in _SHARED},
            }
        )
    return resolved


def load_symbol_map(map_file: str | None, target: str = "") -> SymbolMap | None:
    """Load the configured symbol map, or exit; none traces every scalar found."""
    if not map_file:
        logger.info("[%s] No symbol_map_file: tracing every scalar PLC variable", target)
        return None
    try:
        symbol_map = SymbolMap.from_file(Path(map_file))
    except Exception as e:
        logger.error("[%s] Failed to load symbol map '%s': %s", target, map_file, e)
        sys.exit(1)
    logger.info(
        "[%s] Loaded symbol map '%s' with %d symbols in %d events",
        target,
        map_file,
        len(symbol_map.symbols),
        len(symbol_map.events),
    )
    return symbol_map


def build_clients(targets: list[dict[str, Any]]) -> list[ADSClient]:
    """One client per target, or exit on an inconsistent setting or symbol map."""
    clients = []
    for kwargs in targets:
        kwargs = dict(kwargs)
        name = kwargs["name"]
        symbol_map = load_symbol_map(kwargs.pop("symbol_map_file"), name)
        try:
            clients.append(ADSClient(symbol_map=symbol_map, **kwargs))
        except ValueError as e:
            logger.error("Target '%s': invalid configuration: %s", name, e)
            sys.exit(1)
    return clients


def apply_log_level(level_name: str) -> None:
    """Apply the configured log level, falling back to INFO on a bad value."""
    level = getattr(logging, level_name.upper(), None)
    if not isinstance(level, int):
        logger.warning("Invalid log level '%s', using INFO", level_name)
        level = logging.INFO
    logging.getLogger().setLevel(level)


def open_sources(prefix: str) -> SharedSource | None:
    """The prefix's shared source with the log on its `log` event, following rotation;
    cleared, None and the log on its own source. Earlier records reach stderr only."""
    if not prefix:
        install_log_handler(LOG_SOURCE_NAME)
        logger.info("Trace prefix cleared: one trace source per target")
        return None
    source = zelos_sdk.TraceSource(prefix)
    shared = SharedSource(source, install_log_handler(source))
    logger.info("Trace prefix: %s", prefix)
    return shared


def set_local_router(clients: list[ADSClient], local_ams_net_id: str = "") -> None:
    """The one AMS NetId this process presents, fixed before any client connects, or exit.

    Router-less platforms: `local_ams_net_id`, else derived from the interface
    toward the first target; every target is reached from it. Demo links need none.
    """
    real = [c for c in clients if c._link_factory is None]
    if not real:
        return
    try:
        net_id = ams.ensure_local_router(real[0].host, local_ams_net_id)
    except Exception as e:
        logger.error(
            "Local AMS NetId toward %s: %s; check the host, or set Advanced > Local AMS NetId",
            real[0].host,
            describe_error(e),
        )
        sys.exit(1)
    for client in real:
        client.local_net_id = net_id


def serve(clients: list[ADSClient], prefix: str, local_ams_net_id: str = "") -> None:
    """Publish the action surface, then run every target until shutdown.

    Actions register before `init()`: registered later they may never be
    advertised. The prefix source is a plain TraceSource, not init's global one,
    which the SDK holds for the process: rotation must be able to drop it so its
    segment ends.
    """
    set_local_router(clients, local_ams_net_id)
    runner = ADSRunner(clients)
    ads_actions.set_runner(runner)
    ads_actions.register_actions(zelos_sdk.actions_registry)
    shared = open_sources(prefix)
    zelos_sdk.init(name=ACTION_PREFIX, actions=True)

    for client in clients:
        client.start(shared)
    runner.run()
    code = 1 if runner.failed_at_start else 0
    if runner.wedged:
        # pyads is blocking the worker; a normal exit would join it (atexit) for as
        # long as AdsLib takes. Flush what we can and leave, as the Modbus extension.
        logger.warning(
            "Link call to %s never returned; forcing exit",
            ", ".join(c.name for c in runner.wedged),
        )
        flushed = _flush_traces(clients)
        logger.warning("Forced exit; trace flush %s", "completed" if flushed else "timed out")
        logging.shutdown()
        os._exit(code)
    if code:
        sys.exit(code)


def _flush_traces(clients: list[ADSClient]) -> bool:
    """Flush every trace source for at most FORCED_FLUSH_TIMEOUT; True if that finished.

    On a daemon thread: a flush has no timeout of its own.
    """
    sources = {id(s): s for c in clients if (s := c._source) is not None}
    for handler in logging.getLogger().handlers:
        if isinstance(handler, TraceLoggingHandler) and handler.trace_source is not None:
            sources[id(handler.trace_source)] = handler.trace_source
    done = threading.Event()

    def flush() -> None:
        for source in sources.values():
            source.flush()
        done.set()

    threading.Thread(target=flush, daemon=True).start()
    return done.wait(FORCED_FLUSH_TIMEOUT)


def run_config(config: dict[str, Any], demo: bool = False) -> None:
    """Resolve an app-shaped config (targets[], advanced) and serve it; `demo` (the CLI's
    `demo` / `--demo`) replaces the targets with the built-in demo PLC."""
    advanced = resolve_advanced(config)
    apply_log_level(advanced["log_level"])
    prefix = trace_name(str(advanced["prefix"]))

    if demo:
        from zelos_extension_ads.demo import DEMO_NET_ID, DemoLink, DemoPlc
        from zelos_extension_ads.ethercat import MASTER_PORT

        logger.info("Demo mode: using the built-in demo PLC and its EtherCAT segment")
        config = {
            **config,
            "targets": [{"name": DEMO_NAME, "ams_net_id": DEMO_NET_ID, "ethercat": True}],
        }
    targets = resolve_targets(config, advanced)
    if demo:
        plc = DemoPlc()
        targets[0]["link_factory"] = lambda: DemoLink(plc)
        targets[0]["ethercat_link_factory"] = lambda: DemoLink(plc, MASTER_PORT)

    serve(build_clients(targets), prefix, str(advanced["local_ams_net_id"]).strip())


def run_app_mode(demo: bool = False) -> None:
    """Run the extension with configuration from the Zelos App.

    Args:
        demo: If True, run the built-in demo PLC as the only target
    """
    reject_unknown_shape(read_raw_config())
    try:
        config = load_config()
    except ConfigValidationError as e:
        logger.error("Invalid configuration: %s", "; ".join(m.strip(" •") for m in e.errors))
        sys.exit(1)
    run_config(config, demo)
