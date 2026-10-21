# CLAUDE.md

## Commands

```bash
just install      # deps + pre-commit hooks
just format       # ruff format + ruff check --fix
just check        # ruff check + ruff format --check
just test         # pytest (the wire tests start the demo PLC on 127.0.0.1:48898; skipped if taken)
just hil          # ZELOS_ADS_HOST=<ip> [ZELOS_ADS_NET_ID=..] against a real TwinCAT; writes MAIN.bOut1 only
just dev          # app mode, reads the Zelos App config
just demo         # built-in demo PLC, in-process
just sim          # demo PLC behind AMS/TCP on 127.0.0.1:48898
```

## Key Files

| Path | Role |
|------|------|
| `main.py` | Click entry point; re-exports `ACTION_PREFIX` |
| `zelos_extension_ads/__init__.py` | `ACTION_PREFIX = "Beckhoff ADS"` - must match `name` in `extension.toml` |
| `zelos_extension_ads/ams.py` | NetIds, router-less vs native router, the one local NetId per process, UDP 48899 identify; add route through pyads |
| `zelos_extension_ads/symbols.py` | Symbol / datatype upload parsing, flattening, trace names, values, `SymbolFilter`, `SymbolMap`, `layout` |
| `zelos_extension_ads/transport.py` | `Link` protocol, `PyadsLink` (pyads), error classes (`is_connection_error`, `needs_resync`) |
| `zelos_extension_ads/client.py` | `ADSClient` (one target: connect, upload, transport, reconnect, rotation) and `ADSRunner` (the loop, signals, shutdown, action dispatch) |
| `zelos_extension_ads/actions.py` | Free-function action surface + `register_actions()` |
| `zelos_extension_ads/autoconfig.py` | `auto_config`: identify the form's targets, or broadcast for new ones |
| `zelos_extension_ads/cli/app.py` | Config load, `targets[]` / `advanced` resolution, startup validation, `serve()` |
| `zelos_extension_ads/demo/plc.py` | `DemoPlc` (program, memory, tables, notifications) and `DemoLink` (in-process `Link`) |
| `zelos_extension_ads/demo/server.py` | `DemoServer`: the demo PLC behind AMS/TCP |
| `tests/test_symbols.py` | Tables, flattening, names, filters, values, map |
| `tests/test_client.py` | Client through `DemoLink` (transports, map, actions, config) and over the wire (pyads: notifications, poll, recovery, start failure) |
| `tests/test_hil.py` | Real target, `ZELOS_ADS_HOST` only |

## Architecture

### Startup

- `cli/app.serve()` is the only place that starts clients: config -> clients -> `set_runner` -> `register_actions` -> `open_sources(prefix)` -> `zelos_sdk.init(name=ACTION_PREFIX)` -> `client.start` -> `runner.run()`. Same order as OPC UA; registration must precede `init()`.
- Startup problems (bad config, missing map) are one `logger.error` + `sys.exit(1)`. First contact is required: a target that cannot connect at start stops the extension with one ERROR naming it and the fix.

### Platforms

- `pyads.ads.linux` decides: True = router-less AdsLib (Linux, macOS): `serve()` calls `ensure_local_router` once, before any client starts, which opens the port and sets the local NetId (`advanced.local_ams_net_id`, else the IP toward the first target + `.1.1`); every client presents it. `pyads.Connection.open()` adds the route to the target IP. The target must hold a route back (`add_route` action, which uses the running NetId when there is one).
- False = TwinCAT's router (Beckhoff RT Linux is detected by `/usr/bin/TcSystemServiceUm`; Windows): no local NetId, no routes from here, `set_local_address` raises. Demo mode still works (in-process link); `demo-server` cannot be reached through that router.

### Client

- pyads blocks: every link call runs on the client's one worker thread (`_call`); notifications arrive on AdsLib's thread and are queued, then flushed on the loop after `FLUSH_SECONDS` into one row per (event, PLC timestamp).
- Per connect: `_open` (resolve the target NetId by UDP identify when empty, open, ReadState, device info) -> upload both tables -> `flatten` -> map or filters -> `layout` -> `_declare_events` (added fields rotate the shared source, OPC UA's `_rotate_source`) -> `_start_transport`.
- Jobs on the loop: health (ReadState, logs `_target`, doubles as liveness), poll (sum read), sweep (re-read subscribed symbols silent for `min_update_interval`). Reads are by (index group, index offset, size), never by handle: nothing to release, and a stale address is an ADS error that triggers a re-upload.
- Errors: router/client codes (<1792, >=1856) and OSError = link lost -> disconnect, reconnect (first loss at once, then backoff); `RESYNC_CODES` = addresses stale -> same path; other device errors count, 5 in a row reconnect. TwinCAT's symbol-version notification (0xF008) sets `_resync`.
- Shutdown bounded by `SHUTDOWN_TIMEOUT` (3 s): notifications deleted, link closed, executor abandoned if wedged.

### Actions

- Free functions, `target` optional with one target. Failure convention: raise. `read_symbol` / `write_symbol` resolve against the last upload (every scalar, not only traced ones). `write_symbol` reads back.
- `add_route` and `auto_config` are standalone (run with the extension stopped); the schema's `ui:options.autoconfig` names the latter.

### Demo

- `DemoPlc` ticks at 10 ms on its own thread; `DemoLink` drives it in-process (demo mode, fast tests). `DemoServer` speaks AMS/TCP (READ, WRITE, READWRITE incl. sum read and the three upload index groups, READSTATE, READDEVICEINFO, ADD/DEL notification, DEVICENOTE push) so pyads reaches the same PLC in the wire tests; `stop()` drops its clients like a TwinCAT restart.

## Code Style

- ruff, line length 100, google docstrings
- `contextlib.suppress(Exception)` over bare `try/except: pass`
- comments state the constraint, not the code
