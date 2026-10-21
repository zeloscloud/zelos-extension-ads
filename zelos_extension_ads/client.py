"""ADS client per target with Zelos SDK integration, and the runner that drives them.

pyads blocks, so every request runs on the client's own worker thread (one per
target, requests serialized); the event loop only schedules and logs.
Notifications arrive on the ADS library's receive thread and are handed to the
loop in batches.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import signal
import time
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

import zelos_sdk
from zelos_sdk import schemas
from zelos_sdk.hooks.logging import TraceLoggingHandler

from zelos_extension_ads import ams
from zelos_extension_ads import symbols as sym
from zelos_extension_ads.symbols import HEALTH_EVENT, Field, SymbolFilter, SymbolMap
from zelos_extension_ads.transport import (
    Link,
    PyadsLink,
    describe_error,
    is_connection_error,
    local_router,
    needs_resync,
)

logger = logging.getLogger(__name__)

# Reconnect backoff: a target down for hours costs one attempt a minute.
RECONNECT_INITIAL = 3.0
RECONNECT_MAX = 60.0
# Ceiling on the disconnect at shutdown: a wedged link must not make the process unkillable.
SHUTDOWN_TIMEOUT = 3.0
# Unclassified request failures in a row that force a reconnect: never wedge on a dead link.
POLL_FAILURES_BEFORE_RECONNECT = 5
TRANSPORTS = ("notification", "poll")
# The health read (ReadState) doubles as the liveness probe.
HEALTH_INTERVAL = 1.0
# Notifications are coalesced this long before logging: one row per POU and timestamp.
FLUSH_SECONDS = 0.02
# Staleness sweep steps per min_update_interval.
SWEEP_STEPS = 4
# ADS index group of the symbol version (one byte): read with every health probe and
# subscribed in notification mode; a change means the addresses held are stale.
ADSIGRP_SYM_VERSION = 0xF008
STALE_TABLE = "symbol table changed"

ADS_STATES = {
    0: "INVALID", 1: "IDLE", 2: "RESET", 3: "INIT", 4: "START", 5: "RUN", 6: "STOP",
    7: "SAVECFG", 8: "LOADCFG", 9: "POWERFAILURE", 10: "POWERGOOD", 11: "ERROR",
    12: "SHUTDOWN", 13: "SUSPEND", 14: "RESUME", 15: "CONFIG", 16: "RECONFIG",
}  # fmt: skip

HEALTH_FIELDS = (
    ("state", zelos_sdk.DataType.Int32, ""),
    ("state_name", zelos_sdk.DataType.String, ""),
    ("device_state", zelos_sdk.DataType.Int32, ""),
    ("connected", zelos_sdk.DataType.Boolean, ""),
    ("transport", zelos_sdk.DataType.String, ""),
    ("symbols", zelos_sdk.DataType.UInt32, ""),
    ("notifications", zelos_sdk.DataType.UInt64, ""),
    ("polls", zelos_sdk.DataType.UInt32, ""),
    ("errors", zelos_sdk.DataType.UInt32, ""),
    ("clock_skew_ms", zelos_sdk.DataType.Float64, "ms"),
    ("last_error", zelos_sdk.DataType.String, ""),
)

#: Log event on the prefix source (`<prefix>/log`); a target may not take this name.
LOG_EVENT = "log"
#: Log source when the prefix is cleared.
LOG_SOURCE_NAME = "ads_log"


class ConnectError(Exception):
    """A connect failed for a reason the user can act on; `fix` says how."""

    def __init__(self, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.fix = fix


class StaleTableError(RuntimeError):
    """The target's symbol version moved since the upload: every address held is suspect."""


def default_target_name(host: str) -> str:
    """The host as a trace name: `cx9240`, `192_168_1_10`."""
    return zelos_sdk.sanitize_name(host.strip() or "localhost", kind="source")


class _TargetLog(logging.LoggerAdapter):
    """Prefixes every record with the target name; several targets share one log."""

    def process(self, msg: Any, kwargs: Any) -> tuple[Any, Any]:
        return f"[{self.extra['target']}] {msg}", kwargs


def install_log_handler(source: zelos_sdk.TraceSource | str) -> TraceLoggingHandler:
    """Route INFO and above into the trace; DEBUG would flood it with library chatter."""
    handler = TraceLoggingHandler(source)
    handler.setLevel(logging.INFO)
    logging.getLogger().addHandler(handler)
    return handler


class SharedSource:
    """A trace source and the clients writing it, rotated as one, with the log handler."""

    def __init__(
        self, source: zelos_sdk.TraceSource, log_handler: TraceLoggingHandler | None = None
    ) -> None:
        self.source: zelos_sdk.TraceSource | None = source
        self.clients: list[ADSClient] = []
        self.log_handler = log_handler


Declared = tuple[str, zelos_sdk.DataType, str]


def _field(f: Field) -> Declared:
    return f.name, f.datatype, f.unit


class _Job:
    """A periodic request on the link: health, the poll, the sweep."""

    def __init__(self, period: float, run: Callable[[], Awaitable[None]]) -> None:
        self.due = time.monotonic()
        self.period = period
        self.run = run


class ADSClient:
    """Traces one TwinCAT target's symbols into a trace source; runs in an ADSRunner."""

    def __init__(
        self,
        host: str = "localhost",
        name: str = "",
        ams_net_id: str = "",
        port: int = ams.PLC_PORT,
        symbol_map: SymbolMap | None = None,
        cycle_time: int = 100,
        transport: str = "notification",
        timeout: float = 5.0,
        include: Sequence[str] = (),
        exclude: Sequence[str] = (),
        max_notifications: int = 1000,
        min_update_interval: float = 60.0,
        local_ams_net_id: str = "",
        link_factory: Callable[[], Link] | None = None,
    ) -> None:
        """
        Args:
            host: IP or host name of the TwinCAT router
            name: Target name in the trace and the actions; default the host
            ams_net_id: The target's AMS NetId; empty asks the router on every connect
            port: AMS port of the PLC runtime (851)
            symbol_map: Symbols to trace, grouped; None traces every scalar found
            cycle_time: Notification check period and poll period, ms
            transport: notification or poll
            timeout: ADS request timeout, seconds
            include: Without a map, symbol globs to trace; empty traces everything
            exclude: Without a map, symbol globs not traced; win over include
            max_notifications: Symbols above which the notification transport polls
            min_update_interval: A subscribed symbol silent this long is re-read
            local_ams_net_id: Router-less platforms: the NetId presented; empty derives it
            link_factory: Makes the link (tests, demo); default pyads

        Raises:
            ValueError: If the transport, filters or NetIds are invalid
        """
        if transport not in TRANSPORTS:
            raise ValueError(f"transport must be one of {', '.join(TRANSPORTS)}, got '{transport}'")
        if cycle_time < 1:
            raise ValueError(f"cycle_time must be at least 1 ms, got {cycle_time}")
        if min_update_interval <= 0:
            raise ValueError(f"min_update_interval must be positive, got {min_update_interval}")
        if ams_net_id:
            ams_net_id = ams.parse_net_id(ams_net_id)
        if local_ams_net_id:
            local_ams_net_id = ams.parse_net_id(local_ams_net_id)
        self.host = host.strip() or "localhost"
        self.name = name or default_target_name(self.host)
        self.ams_net_id = ams_net_id
        self.port = port
        self.symbol_map = symbol_map
        self.cycle_time = cycle_time
        self.transport = transport
        self.timeout = timeout
        self.filters = SymbolFilter.parse(include, exclude)
        self.max_notifications = max_notifications
        self.min_update_interval = min_update_interval
        self.local_ams_net_id = local_ams_net_id
        self._link_factory = link_factory
        self._log = _TargetLog(logger, {"target": self.name})

        self._link: Link | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"ads-{self.name}")
        self.in_call = False  # the worker is inside a link call (set on the worker)
        self._stop_event: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self._connected = False
        self.last_error: str | None = None
        self.fix = ""
        self.ever_connected = False
        self._at_start = False
        self._contacted = asyncio.Event()
        self.net_id = ams_net_id  # resolved per connect when empty
        self.local_net_id = ""
        self.device = ("", "")  # name, version
        self.plc_state = (0, 0)

        # Every scalar the target holds (by lower-cased symbol), and what is traced.
        self._uploaded: dict[str, Field] = {}
        self._events: dict[str, list[Field]] = {}
        self._traced: dict[str, Field] = {}
        # Transport state per connection.
        self._handles: dict[int, Field] = {}
        self._polled: list[Field] = []
        self._active = ""  # transport in use this connection
        self._pending: collections.deque[tuple[Field, int, bytes]] = collections.deque()
        self._flush_handle: asyncio.TimerHandle | None = None
        self._seen: dict[str, float] = {}
        self._failed: set[str] = set()
        self._version: bytes | None = None
        self._resync = ""
        self._jobs: list[_Job] = []
        self._notification_count = 0
        self._poll_count = 0
        self._error_count = 0
        self._skew_ms = 0.0

        # Trace plumbing; see _init_trace_source / _rotate_source.
        self._shared: SharedSource | None = None
        self._trace_events: dict[str, Any] = {}
        self._declared: dict[str, list[Declared]] = {}
        self._schemas: dict[str, dict[str, int]] = {}
        self._event_prefix = ""

    # ─── Trace source ───────────────────────────────────────────────────────

    @property
    def _source(self) -> zelos_sdk.TraceSource | None:
        return self._shared.source if self._shared else None

    def start(self, shared: SharedSource | None = None) -> None:
        """Declare the health event; `shared` is the prefix source, None for the target's own."""
        self._running = True
        if shared is None:
            self._shared, self._event_prefix = SharedSource(zelos_sdk.TraceSource(self.name)), ""
        else:
            self._shared, self._event_prefix = shared, f"{self.name}/"
        if self not in self._shared.clients:
            self._shared.clients.append(self)
        self._trace_events, self._declared, self._schemas = {}, {}, {}
        self._declare(HEALTH_EVENT, list(HEALTH_FIELDS))
        self._log.info("Client started (%s, AMS port %d)", self.host, self.port)

    def _declare(self, event: str, fields: list[Declared]) -> None:
        assert self._source is not None
        self._declared[event] = fields
        self._trace_events[event] = self._source.add_event(
            f"{self._event_prefix}{event}", [zelos_sdk.TraceEventFieldMetadata(*f) for f in fields]
        )

    def _declare_events(self, events: dict[str, list[Field]]) -> tuple[list[str], list[str]]:
        """Declare new events; record fields added to declared ones (a rotation
        follows) and drop fields whose datatype changed (types are not reconciled).

        Returns:
            (fields added as `event/name`, fields left out for a type change)
        """
        added, changed = [], []
        for event, fields in events.items():
            schema = self._schemas.get(event)
            if schema is None:
                self._schemas[event] = {f.name: f.data_type for f in fields}
                self._declare(event, [_field(f) for f in fields])
                continue
            keep = []
            for f in fields:
                if schema.get(f.name, f.data_type) != f.data_type:
                    changed.append(f.key)
                    continue
                if f.name not in schema:
                    schema[f.name] = f.data_type
                    self._declared[event].append(_field(f))
                    added.append(f.key)
                keep.append(f)
            events[event] = keep
        return added, changed

    def _rotate_source(self) -> None:
        """Move every client on this source to a new segment of the same name.

        A declared schema is fixed per TraceSource instance; added fields need a
        new one. Synchronous: no await between the last write and the switch.
        """
        shared = self._shared
        assert shared is not None and shared.source is not None
        handler = shared.log_handler
        with handler.lock if handler else contextlib.nullcontext():
            name = shared.source.name
            shared.source.flush()
            shared.source = None
            for client in shared.clients:
                client._trace_events = {}
            if handler:
                handler.trace_source = handler.trace_event = None
            source = zelos_sdk.TraceSource(name)
            events = {
                client: {
                    event: source.add_event(
                        f"{client._event_prefix}{event}",
                        [zelos_sdk.TraceEventFieldMetadata(*f) for f in fields],
                    )
                    for event, fields in client._declared.items()
                }
                for client in shared.clients
            }
            shared.source = source
            for client, client_events in events.items():
                client._trace_events = client_events
            if handler:
                handler.trace_source = source
                handler.trace_event = source.add_event(LOG_EVENT, schemas.Log)

    # ─── Connection ─────────────────────────────────────────────────────────

    async def _call(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run a blocking link call on this client's worker thread."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, partial(self._blocking, fn, args))

    def _blocking(self, fn: Callable[..., Any], args: tuple[Any, ...]) -> Any:
        """Worker thread: `in_call` tells shutdown whether a call never came back."""
        self.in_call = True
        try:
            return fn(*args)
        finally:
            self.in_call = False

    def _open(self) -> None:
        """Worker thread: local router, NetId, link, first request."""
        net_id = self.ams_net_id
        if self._link_factory is None:
            self.local_net_id = local_router(self.host, self.local_ams_net_id)
        if not net_id and self._link_factory is None:
            device = ams.identify_one(self.host, min(self.timeout, 3.0))
            if device is None:
                raise ConnectError(
                    f"no TwinCAT router answered at {self.host} (UDP 48899)",
                    "check the host, or set its AMS NetId",
                )
            net_id = device.net_id
            self._log.info(
                "%s is %s (%s, TwinCAT %s)", self.host, net_id, device.name, device.version
            )
        self.net_id = net_id
        link = (
            self._link_factory()
            if self._link_factory
            else PyadsLink(self.host, net_id, self.port, self.timeout)
        )
        link.open()
        try:
            self.plc_state = link.read_state()
            self.device = link.device_info()
        except BaseException:
            with contextlib.suppress(Exception):
                link.close()
            raise
        self._link = link

    async def connect(self) -> bool:
        """Open, upload the symbol table, declare and subscribe; False on failure (logged)."""
        try:
            await self._call(self._open)
            assert self._link is not None
            self._handles, self._polled, self._failed, self._seen = {}, [], set(), {}
            self._pending.clear()
            self._resync = ""
            started = time.monotonic()
            # Version first: a table that moves during the upload still shows as changed.
            self._version = await self._call(self._read_version)
            raw_symbols, raw_types = await self._call(self._link.upload)
            fields = sym.flatten(sym.parse_symbols(raw_symbols), sym.parse_types(raw_types))
            self._uploaded = {f.symbol.lower(): f for f in fields}
            events = self._select(fields)
            added, changed = self._declare_events(events)
            for key in changed:
                self._log.warning("Field %s changed datatype; skipped until restart", key)
            if added:
                self._log.info("New fields %s: new trace segment", ", ".join(added))
                self._rotate_source()
            self._events = events
            self._traced = {f.symbol.lower(): f for fs in events.values() for f in fs}
            self._log.info(
                "Symbol table: %d symbols, %d traced in %d events (%.1fs)",
                len(fields),
                len(self._traced),
                len(events),
                time.monotonic() - started,
            )
            await self._start_transport()
            self._connected = True
            self.last_error, self.fix, self.ever_connected = None, "", True
            self._log.info(
                "Connected to %s (%s:%d, %s %s, %s)",
                self.host,
                self.net_id,
                self.port,
                self.device[0],
                self.device[1],
                ADS_STATES.get(self.plc_state[0], str(self.plc_state[0])),
            )
            return True
        except Exception as e:
            self._connected = False
            self.last_error = self._connect_failure(e)
            await self._close_link()
            if not self._at_start:  # at start the runner reports it
                hint = f"; {self.fix}" if self.fix else ""
                self._log.warning("Connection to %s failed: %s%s", self.host, self.last_error, hint)
            return False

    def _select(self, fields: list[Field]) -> dict[str, list[Field]]:
        """What this connection traces: the map's fields, or every scalar the filters keep."""
        if self.symbol_map is not None:
            traced, missing = self.symbol_map.apply(fields)
            if missing:
                self._log.error(
                    "%d mapped symbols not on the target: %s", len(missing), ", ".join(missing[:10])
                )
        else:
            traced = [f for f in fields if self.filters.keeps(f.symbol)]
            if fields and not traced:
                self._log.warning("No symbol left after include/exclude; nothing traced")
        events, dropped = sym.layout(traced)
        if dropped:
            self._log.warning(
                "%d symbols share a trace name with another and are skipped: %s",
                len(dropped),
                ", ".join(dropped[:10]),
            )
        return events

    def _connect_failure(self, error: Exception) -> str:
        """Why a connect failed, with `fix` set when the user can act on it."""
        self.fix = ""
        if isinstance(error, ConnectError):
            self.fix = error.fix
            return str(error)
        code = getattr(error, "err_code", None)
        local = self.local_net_id or "this machine's NetId"
        if code == 6:
            self.fix = (
                f"check the AMS port, or add a route for {local} on the target (add_route action)"
            )
            return (
                f"ADS port {self.port} not reachable on {self.net_id}: no PLC runtime there, "
                f"or the target holds no route for {local}"
            )
        if code == 7:
            self.fix = "check host and ams_net_id"
            return f"no route to {self.net_id} at {self.host}"
        if code == 1861:
            self.fix = (
                f"check that {self.host} is reachable on TCP 48898, and that it holds a route "
                f"for {local} (add_route action)"
            )
            return f"no answer from {self.host}"
        return describe_error(error)

    async def _close_link(self) -> None:
        link, self._link = self._link, None
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        self._handles = {}
        if link is not None:
            with contextlib.suppress(Exception):
                await self._call(link.close)

    async def disconnect(self) -> None:
        """Close the link, ignoring errors from an already-dead socket."""
        was = self._connected
        self._connected = False
        await self._close_link()
        if was:
            self._log_health(connected=False)
            self._log.info("Disconnected from %s", self.host)

    async def _ensure_connected(self) -> bool:
        if self._connected and self._link is not None:
            return True
        if self._resync:
            self._log.warning("%s; uploading the symbol table again", self._resync)
        await self._close_link()
        self._log.info("Connecting to %s...", self.host)
        return await self.connect()

    # ─── Transport ──────────────────────────────────────────────────────────

    async def _start_transport(self) -> None:
        assert self._link is not None
        fields = [f for fs in self._events.values() for f in fs]
        self._jobs = [_Job(max(HEALTH_INTERVAL, self.cycle_time / 1000.0), self._poll_health)]
        self._active = self.transport
        if self.transport == "notification" and len(fields) > self.max_notifications:
            self._log.warning(
                "%d symbols exceed max_notifications (%d): polling this target",
                len(fields),
                self.max_notifications,
            )
            self._active = "poll"
        if self._active == "notification":
            self._loop = asyncio.get_running_loop()
            self._polled = await self._call(self._subscribe_all, fields)
            with contextlib.suppress(Exception):
                await self._call(
                    self._link.subscribe, ADSIGRP_SYM_VERSION, 0, 1, 1000, self._on_version
                )
            if self._handles:
                self._jobs.append(_Job(self.min_update_interval / SWEEP_STEPS, self._sweep))
        else:
            self._polled = fields
        if self._polled:
            self._jobs.append(_Job(self.cycle_time / 1000.0, self._poll))
        self._log.info(
            "Transport %s: %d subscribed, %d polled, cycle %d ms",
            self._active,
            len(self._handles),
            len(self._polled),
            self.cycle_time,
        )

    def _subscribe_all(self, fields: list[Field]) -> list[Field]:
        """Worker thread: one notification per field; the rest are polled after a refusal."""
        assert self._link is not None
        for i, f in enumerate(fields):
            try:
                handle = self._link.subscribe(
                    f.index_group,
                    f.index_offset,
                    f.size,
                    self.cycle_time,
                    partial(self._on_notification, f),
                )
            except Exception as e:
                if is_connection_error(e):
                    raise
                self._log.warning(
                    "Notification refused for %s (%s); polling it and the %d after it",
                    f.symbol,
                    describe_error(e),
                    len(fields) - i - 1,
                )
                return fields[i:]
            self._handles[handle] = f
        return []

    def _on_notification(self, f: Field, stamp_ns: int, data: bytes) -> None:
        """ADS receive thread: queue and wake the loop."""
        self._pending.append((f, stamp_ns, data))
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._kick)

    def _read_version(self) -> bytes | None:
        """Worker thread: the symbol version, None on a target without one (logged once)."""
        assert self._link is not None
        try:
            return self._link.read(ADSIGRP_SYM_VERSION, 0, 1)
        except Exception as e:
            if is_connection_error(e):
                raise
            self._log.warning(
                "Symbol version unreadable (%s): a program change is only caught by its "
                "stale-address errors",
                describe_error(e),
            )
            return None

    def _changed(self, version: bytes | None) -> bool:
        """Whether `version` differs from the table uploaded; `_version` is set per connect only."""
        return self._version is not None and version is not None and version != self._version

    def _on_version(self, _stamp_ns: int, data: bytes) -> None:
        """ADS receive thread: the loop reconnects on a change."""
        if self._changed(data):
            self._resync = STALE_TABLE
            loop = self._loop
            if loop is not None and not loop.is_closed():
                loop.call_soon_threadsafe(self._kick)

    def _kick(self) -> None:
        if self._flush_handle is None and self._loop is not None:
            self._flush_handle = self._loop.call_later(FLUSH_SECONDS, self._flush)

    def _flush(self) -> None:
        """Log the queued notifications: one row per event and PLC timestamp."""
        self._flush_handle = None
        if not self._connected:
            self._pending.clear()
            return
        rows: dict[tuple[str, int], dict[str, Any]] = {}
        now = time.monotonic()
        latest = 0
        while self._pending:
            f, stamp_ns, data = self._pending.popleft()
            value = sym.decode(data, f.data_type, f.scale)
            if value is None:
                self._log_field_failure(f, f"{len(data)} bytes for a {f.type_name}")
                continue
            rows.setdefault((f.event, stamp_ns), {})[f.name] = value
            self._seen[f.key] = now
            self._notification_count += 1
            latest = max(latest, stamp_ns)
        for (event, stamp_ns), values in sorted(rows.items(), key=lambda r: r[0][1]):
            trace_event = self._trace_events.get(event)
            if trace_event is not None:
                trace_event.log_at(stamp_ns, **values)
        if latest:
            self._skew_ms = (time.time_ns() - latest) / 1e6
        if self._resync:
            self._connected = False  # the loop reconnects and uploads again

    async def _read_fields(self, fields: list[Field]) -> None:
        """One sum read of `fields`, logged at host time, one row per event."""
        assert self._link is not None
        results = await self._call(
            self._link.sum_read, [(f.index_group, f.index_offset, f.size) for f in fields]
        )
        rows: dict[str, dict[str, Any]] = {}
        now = time.monotonic()
        for f, result in zip(fields, results, strict=True):
            if isinstance(result, int):
                self._log_field_failure(f, f"ADS error {result}")
                continue
            value = sym.decode(result, f.data_type, f.scale)
            if value is None:
                self._log_field_failure(f, f"{len(result)} bytes for a {f.type_name}")
                continue
            rows.setdefault(f.event, {})[f.name] = value
            self._seen[f.key] = now
        for event, values in rows.items():
            trace_event = self._trace_events.get(event)
            if trace_event is not None and values:
                trace_event.log(**values)
        self._poll_count += 1

    async def _poll(self) -> None:
        await self._read_fields(self._polled)

    async def _sweep(self) -> None:
        """Re-read subscribed symbols silent for min_update_interval: a static value
        is logged at least that often."""
        cutoff = time.monotonic() - self.min_update_interval
        stale = [f for f in self._handles.values() if self._seen.get(f.key, 0.0) < cutoff]
        if stale:
            await self._read_fields(stale)

    async def _poll_health(self) -> None:
        """ReadState, and the symbol version: polled addresses go stale silently."""
        assert self._link is not None
        self.plc_state = await self._call(self._link.read_state)
        if self._version is not None and self._changed(await self._call(self._read_version)):
            self._resync, self._connected = STALE_TABLE, False
            return
        self._log_health(connected=True)

    def _log_health(self, connected: bool) -> None:
        event = self._trace_events.get(HEALTH_EVENT)
        if event is None:
            return
        state, device_state = self.plc_state
        event.log(
            state=state,
            state_name=ADS_STATES.get(state, str(state)),
            device_state=device_state,
            connected=connected,
            transport=self._active,
            symbols=len(self._traced),
            notifications=self._notification_count,
            polls=self._poll_count,
            errors=self._error_count,
            clock_skew_ms=self._skew_ms,
            last_error=self.last_error or "",
        )

    def _log_field_failure(self, f: Field, reason: str) -> None:
        """One ERROR per bad symbol per connection: per cycle would flood the log and the trace."""
        self._error_count += 1
        if f.symbol in self._failed:
            return
        self._failed.add(f.symbol)
        self._log.error(
            "Symbol '%s' unreadable: %s - further reports suppressed this connection",
            f.symbol,
            reason,
        )

    # ─── Lifecycle ──────────────────────────────────────────────────────────

    async def _run_async(self, stop_event: asyncio.Event) -> None:
        """Run this target's jobs, one request at a time, until `stop_event`.

        State is per client: one dead target never stalls another in the loop.
        """
        self._stop_event = stop_event
        self._loop = asyncio.get_running_loop()
        self._at_start = not self.ever_connected
        backoff = RECONNECT_INITIAL
        losses = 0
        failures = 0
        try:
            while self._running and not stop_event.is_set():
                # Raced: a black-holed connect sits far past the manifest's grace.
                _, connected = await self._or_stop(self._ensure_connected())
                first, self._at_start = self._at_start, False
                self._contacted.set()
                if not connected:
                    if stop_event.is_set() or first:
                        break  # at start the runner stops the extension
                    self._log.warning("Retrying %s in %.0fs", self.host, backoff)
                    if await self._wait_or_stop(backoff):
                        break
                    backoff = min(backoff * 2, RECONNECT_MAX)
                    continue

                job = min(self._jobs, key=lambda j: j.due)
                if await self._wait_or_stop(max(0.0, job.due - time.monotonic())):
                    break
                if not self._connected:
                    continue  # a notification or probe found the table stale: reconnect
                # Behind schedule, the next run starts from now rather than bursting.
                job.due = max(job.due + job.period, time.monotonic())
                try:
                    if (await self._or_stop(job.run()))[0]:
                        break
                    backoff = RECONNECT_INITIAL
                    losses = 0
                    failures = 0
                except Exception as e:
                    self._error_count += 1
                    if is_connection_error(e) or needs_resync(e):
                        why = "symbol addresses stale" if needs_resync(e) else "lost"
                        self._log.warning(
                            "Connection to %s %s: %s", self.host, why, describe_error(e)
                        )
                        self.last_error = describe_error(e)
                        await self.disconnect()
                        losses += 1
                        # The first loss reconnects at once; from the second, back off.
                        if losses > 1:
                            if await self._wait_or_stop(backoff):
                                break
                            backoff = min(backoff * 2, RECONNECT_MAX)
                        continue
                    self._log.error("Request failed: %s", describe_error(e))
                    failures += 1
                    if failures >= POLL_FAILURES_BEFORE_RECONNECT:
                        self._log.warning("%d failures in a row, reconnecting", failures)
                        await self.disconnect()
                        failures = 0
        finally:
            self._at_start = False
            self._contacted.set()
            self._running = False
            try:
                await asyncio.wait_for(self.disconnect(), SHUTDOWN_TIMEOUT)
            except TimeoutError:
                self._log.warning("Disconnect exceeded %.0fs, abandoning link", SHUTDOWN_TIMEOUT)
            self._executor.shutdown(wait=False)

    async def _or_stop(self, coro: Awaitable[Any]) -> tuple[bool, Any]:
        """(True, None) if shutdown wins, `coro` cancelled; else (False, its result)."""
        assert self._stop_event is not None
        work = asyncio.ensure_future(coro)
        stop = asyncio.ensure_future(self._stop_event.wait())
        try:
            await asyncio.wait({work, stop}, return_when=asyncio.FIRST_COMPLETED)
            if work.done():
                return False, work.result()
            return True, None
        finally:
            stop.cancel()
            if not work.done():
                work.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await work

    async def _wait_or_stop(self, seconds: float) -> bool:
        """Wait up to `seconds`; True if shutdown was requested meanwhile."""
        assert self._stop_event is not None
        try:
            await asyncio.wait_for(self._stop_event.wait(), seconds)
        except TimeoutError:
            return False
        return True

    # ─── Action support ─────────────────────────────────────────────────────

    def field(self, symbol: str) -> Field:
        """The scalar `symbol` as uploaded on the last connect; ValueError if unknown."""
        f = self._uploaded.get(symbol.strip().lower())
        if f is None:
            if not self._uploaded:
                raise RuntimeError(f"target '{self.name}' has not connected yet")
            raise ValueError(
                f"Unknown symbol '{symbol}' on target '{self.name}'; scalars only "
                "(struct members as MAIN.stX.member, array elements as MAIN.arr[0]). "
                "Use list_symbols."
            )
        return f

    def symbols(self, pattern: str = "") -> list[dict[str, Any]]:
        """Every scalar uploaded, with its trace address when traced."""
        out = []
        for f in self._uploaded.values():
            if pattern and not sym.SymbolFilter.parse([pattern]).keeps(f.symbol):
                continue
            traced = self._traced.get(f.symbol.lower())
            out.append(
                {
                    "symbol": f.symbol,
                    "type": f.type_name,
                    "size": f.size,
                    "comment": f.comment,
                    "event": traced.event if traced else "",
                    "field": traced.name if traced else "",
                }
            )
        return out

    def _require_link(self) -> Link:
        if self._resync:
            raise StaleTableError(f"{STALE_TABLE} on '{self.name}'; reconnecting, retry")
        if self._link is None or not self._connected:
            raise RuntimeError(f"target '{self.name}' is not connected")
        return self._link

    async def read_value(self, f: Field) -> Any:
        data = await self._call(self._require_link().read, f.index_group, f.index_offset, f.size)
        return sym.decode(data, f.data_type)

    async def write_value(self, f: Field, value: Any) -> Any:
        """Write `value` and read it back; refused when the symbol version moved."""
        data = sym.encode(value, f.data_type, f.size)
        link = self._require_link()
        try:
            raw = await self._call(self._write_checked, link, f, data)
        except StaleTableError:
            self._resync, self._connected = STALE_TABLE, False
            raise
        return sym.decode(raw, f.data_type)

    def _write_checked(self, link: Link, f: Field, data: bytes) -> bytes:
        """Worker thread: version check, write and read-back with no other request between."""
        if self._changed(self._read_version()):
            raise StaleTableError(f"{STALE_TABLE} on '{self.name}'; write refused, retry")
        link.write(f.index_group, f.index_offset, data)
        return link.read(f.index_group, f.index_offset, f.size)

    async def read_state(self) -> dict[str, Any]:
        self.plc_state = await self._call(self._require_link().read_state)
        state, device_state = self.plc_state
        return {
            "target": self.name,
            "ads_state": state,
            "ads_state_name": ADS_STATES.get(state, str(state)),
            "device_state": device_state,
            "device": self.device[0],
            "version": self.device[1],
        }

    @property
    def state(self) -> str:
        """ok, connecting (the first connect) or disconnected (was up, retrying)."""
        if self._connected:
            return "ok"
        return "disconnected" if self.ever_connected else "connecting"

    def status(self) -> dict[str, Any]:
        state, device_state = self.plc_state
        return {
            "target": self.name,
            "host": self.host,
            "ams_net_id": self.net_id,
            "port": self.port,
            "connected": self._connected,
            "state": self.state,
            "last_error": self.last_error,
            "plc_state": ADS_STATES.get(state, str(state)) if self._connected else "",
            "device": self.device[0],
            "version": self.device[1],
            "transport": self._active or self.transport,
            "cycle_time": self.cycle_time,
            "symbols": len(self._uploaded),
            "traced": len(self._traced),
            "subscribed": len(self._handles),
            "polled": len(self._polled),
            "notifications": self._notification_count,
            "polls": self._poll_count,
            "errors": self._error_count,
            "clock_skew_ms": round(self._skew_ms, 3),
            "local_ams_net_id": self.local_net_id,
            "device_state": device_state,
        }


class ADSRunner:
    """The one event loop every client runs in: signals, stop, action dispatch.

    Disconnects run concurrently: shutdown costs SHUTDOWN_TIMEOUT in total, not per target.
    """

    def __init__(self, clients: Sequence[ADSClient]) -> None:
        """`clients`: one per target, names unique (checked at config load)."""
        self.clients = {c.name: c for c in clients}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event = asyncio.Event()
        # Targets whose first connect failed: the caller exits 1.
        self.failed_at_start: list[ADSClient] = []
        # Targets whose worker is still inside a link call after shutdown: the caller
        # must not wait for it (atexit would join the thread).
        self.wedged: list[ADSClient] = []

    def is_running(self) -> bool:
        return self._loop is not None and self._loop.is_running()

    def run(self) -> None:
        """Run every client until shutdown (blocking)."""
        asyncio.run(self._run_async())

    async def _run_async(self) -> None:
        self._loop = asyncio.get_running_loop()
        remove_signal_handlers = self._install_signal_handlers()
        tasks = [asyncio.create_task(c._run_async(self._stop_event)) for c in self.clients.values()]
        try:
            if await self._failed_at_start():
                return
            await asyncio.gather(*tasks)
        finally:
            # A client that raised is a bug; stop the rest cleanly.
            self._stop_event.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            remove_signal_handlers()
            self.wedged = [c for c in self.clients.values() if c.in_call]

    async def _failed_at_start(self) -> bool:
        """After every client's first connect, log each target that failed it.

        First contact is a hard requirement: a target that cannot be reached or
        refuses us at start is a config error, fixed before starting again. Once
        a target has connected, drops go through the reconnect backoff.
        """
        first = asyncio.gather(*(c._contacted.wait() for c in self.clients.values()))
        stop = asyncio.ensure_future(self._stop_event.wait())
        try:
            await asyncio.wait({first, stop}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            first.cancel()
            stop.cancel()
        if self._stop_event.is_set():
            return False
        self.failed_at_start = [c for c in self.clients.values() if not c._connected]
        for c in self.failed_at_start:
            hint = f"; {c.fix}, then start the extension again" if c.fix else ""
            logger.error(
                "Target '%s' (%s): cannot connect: %s%s", c.name, c.host, c.last_error, hint
            )
        return bool(self.failed_at_start)

    def stop(self) -> None:
        """Request shutdown. Safe from any thread, including a signal handler."""
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._stop_event.set)
        else:
            self._stop_event.set()
        logger.info("ADS extension stopping")

    def _install_signal_handlers(self) -> Callable[[], None]:
        """Set the stop event on SIGTERM/SIGINT; returns the uninstaller.

        sys.exit from a signal.signal handler would drop the links uncleanly.
        """
        loop, event = self._loop, self._stop_event
        if loop is None:
            return lambda: None

        def request_stop() -> None:
            logger.info("Shutdown signal received, stopping ADS extension")
            event.set()

        installed: list[int] = []
        replaced: dict[int, Any] = {}
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, request_stop)
                installed.append(sig)
            except (NotImplementedError, RuntimeError, ValueError):
                # Windows, or off the main thread.
                with contextlib.suppress(ValueError, OSError):
                    replaced[sig] = signal.signal(
                        sig, lambda *_: loop.call_soon_threadsafe(request_stop)
                    )

        def remove() -> None:
            for sig in installed:
                with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                    loop.remove_signal_handler(sig)
            for sig, prior in replaced.items():
                with contextlib.suppress(ValueError, OSError, TypeError):
                    signal.signal(sig, prior if prior is not None else signal.SIG_DFL)

        return remove

    def _run_coro(self, coro: Coroutine[Any, Any, Any], timeout: float) -> Any:
        """Run an action's coroutine in the live loop, on the client's link.

        Raises:
            RuntimeError: If the loop is not running
            TimeoutError: If the coroutine outlives `timeout`, having cancelled it
        """
        if self._loop is None or not self._loop.is_running():
            coro.close()
            raise RuntimeError("extension is not running")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            raise
