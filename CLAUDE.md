# CLAUDE.md

## Commands

```bash
just install      # deps + pre-commit hooks
just format       # ruff format + ruff check --fix
just check        # ruff check + ruff format --check
just test         # pytest (the wire tests start the demo PLC on 127.0.0.1:48898; skipped if taken)
just hil          # ZELOS_ADS_HOST=<ip> [ZELOS_ADS_NET_ID=..] against a real TwinCAT; writes MAIN.bOut1 only
just run          # app mode, reads the Zelos App config
just demo         # built-in demo PLC, in-process
just sim          # demo PLC behind AMS/TCP on 127.0.0.1:48898
```

## Key Files

| Path | Role |
|------|------|
| `main.py` | Click entry point; re-exports `ACTION_PREFIX`, which the standalone action inventory reads from the entry module |
| `zelos_extension_ads/__init__.py` | `ACTION_PREFIX = "ADS"` - must match `name` in `extension.toml` |
| `zelos_extension_ads/ams.py` | NetIds, router-less vs native router, the one local NetId per process, UDP 48899 identify; add route through pyads |
| `zelos_extension_ads/symbols.py` | Symbol / datatype upload parsing, flattening, trace names, values, `SymbolFilter`, `SymbolMap`, `layout` |
| `zelos_extension_ads/transport.py` | `Link` protocol, `PyadsLink` (pyads), error classes (`is_connection_error`, `needs_resync`); `read_port` reads another AMS port over the same link |
| `zelos_extension_ads/ethercat.py` | EtherCAT master ADS interface: index groups, state and link names, `read_snapshot`, product names, CoE SDO read and decode |
| `zelos_extension_ads/client.py` | `ADSClient` (one target: connect, upload, transport, reconnect, rotation) and `ADSRunner` (the loop, signals, shutdown, action dispatch) |
| `zelos_extension_ads/actions.py` | Free-function action surface + `register_actions()` |
| `zelos_extension_ads/autoconfig.py` | `auto_config`: identify the form's targets, or broadcast for new ones |
| `zelos_extension_ads/cli/app.py` | Config load, `targets[]` / `advanced` resolution, startup validation, `serve()` |
| `zelos_extension_ads/demo/plc.py` | `DemoPlc` (program, memory, tables, notifications) and `DemoLink` (in-process `Link`) |
| `zelos_extension_ads/demo/server.py` | `DemoServer`: the demo PLC behind AMS/TCP |
| `tests/test_symbols.py` | Tables, flattening, names, filters, values, map |
| `tests/test_client.py` | Client through `DemoLink` (transports, map, actions, config) and over the wire (pyads: notifications, poll, recovery, start failure) |
| `tests/test_ethercat.py` | EtherCAT trace and actions through the demo master |
| `tests/test_hil.py` | Real target, `ZELOS_ADS_HOST` only |

## Architecture

### Startup

- `cli/app.serve()` is the only place that starts clients: config -> clients -> `set_local_router` -> `set_runner` -> `register_actions` -> `open_sources(prefix)` -> `zelos_sdk.init(name=ACTION_PREFIX)` -> `client.start` -> `runner.run()`. Registration must precede `init()`.
- Startup problems (bad config, missing map) are one `logger.error` + `sys.exit(1)`. First contact is required: a target that cannot connect at start stops the extension with one ERROR naming it and the fix.

### Platforms

- `pyads.ads.linux` decides: True = router-less AdsLib (Linux, macOS): `serve()` calls `set_local_router`, which runs `ams.ensure_local_router` once before any client starts, which opens the port and sets the local NetId (`advanced.local_ams_net_id`, else the IP toward the first target + `.1.1`); every client presents it. `pyads.Connection.open()` adds the route to the target IP. The target must hold a route back (`add_route` action, which uses the running NetId when there is one).
- False = TwinCAT's router (Beckhoff RT Linux is detected by `/usr/bin/TcSystemServiceUm`; Windows): no local NetId, no routes from here, `set_local_address` raises. Demo mode still works (in-process link); `demo-server` cannot be reached through that router.

### Client

- pyads blocks: every link call runs on the client's one worker thread (`_call`); notifications are one per symbol-table entry (`sym.blocks`: a span over its traced leaves, decoded per leaf; a span over `NOTIFY_MAX_BYTES` is polled) and arrive on AdsLib's thread, queued, then flushed on the loop after `FLUSH_SECONDS` into one row per (event, PLC timestamp).
- Per connect: `_open` (resolve the target NetId by UDP identify when empty, open, ReadState, device info) -> upload both tables -> `flatten` -> map or filters -> `layout` -> `_declare_events` (added fields rotate the shared source, `_rotate_source`) -> `_start_transport`.
- Jobs on the loop: health (ReadState, logs `_target`, doubles as liveness), poll (sum read), sweep (re-read subscribed symbols silent for `min_update_interval`). Reads are by (index group, index offset, size), never by handle: nothing to release, and a stale address is an ADS error that triggers a re-upload. Writes go by symbol handle (0xF003 get, 0xF005 write and read back, 0xF006 release in a `finally`): the target resolves the name at write time.
- Errors: router/client codes (<1792, >=1856) and OSError = link lost -> disconnect, reconnect (first loss at once, then backoff); `RESYNC_CODES` = addresses stale -> same path; other device errors count, 5 in a row reconnect. Watched values (`_watched`: symbol version 0xF008, and `OnlineChangeCnt` resolved from the full upload, tracing filters aside) are captured per connect, read by the health job and subscribed in notification mode (counted in `max_notifications`); a change sets `_resync`.
- Shutdown bounded by `SHUTDOWN_TIMEOUT` (3 s): notifications deleted, link closed, executor abandoned if wedged.
- EtherCAT (`ethercat=True`): a second `Link` to the master device (`ethercat_ams_net_id`, default the first EtherCAT device the runtime's I/O server lists: AMS port 300, group 0x5000 offsets 2 count / 1 ids, 0x5000+id offsets 7 type (94) / 5 NetId / 1 name; AMS port 0xFFFF), opened in `_open` after the PLC link (a master that does not answer fails the connect with the fix), closed with it. One `_poll_ethercat` job on the same worker every `ethercat_interval`: `ethercat.read_snapshot` (master state 0x3/0x100, counters 0xC, count 0x6 (u16), addresses 0x7, states 0x9, identity 0x11 cached per address per connection, CRC 0x12), the `_ethercat` row every read, per-slave events declared on first sight and logged on change or every `min_update_interval`. Errors go through the job loop's classification like any request. `read_sdo` uses `Link.read_port` on the slave's station address (0xF302); ADS 13 = no mailbox.

### Actions

- Free functions, `target` optional with one target. Failure convention: raise. `read_symbol` / `write_symbol` resolve the type against the last upload (every scalar, not only traced ones). `write_symbol` writes by handle and reads back through it; ADS 1808 (symbol gone) is a ValueError.
- `add_route` and `auto_config` are standalone (run with the extension stopped); the schema's `ui:options.autoconfig` names the latter.

### Demo

- `DemoPlc` ticks at 10 ms on its own thread; `DemoLink` drives it in-process (demo mode, fast tests), `DemoLink(plc, MASTER_PORT)` its `DemoEtherCAT` segment (EK1100, EL1004, EL2004, EL3004 with CoE; `drop(address)` for tests). `DemoServer` speaks AMS/TCP (READ, WRITE, READWRITE incl. sum read, the three upload index groups and symbol handles (0xF003/5/6), READSTATE, READDEVICEINFO, ADD/DEL notification, DEVICENOTE push) so pyads reaches the same PLC in the wire tests; `stop()` drops its clients like a TwinCAT restart.

## Code Style

- ruff, line length 100, google docstrings
- `contextlib.suppress(Exception)` over bare `try/except: pass`
- comments state the constraint, not the code
