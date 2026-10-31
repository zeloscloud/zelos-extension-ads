# Contributing

## Prerequisites

- [Zelos](https://zeloscloud.io) 26.0.10 or later
- [Zelos CLI](https://docs.zeloscloud.io/reference/cli/) 0.1.11 or later
- [just](https://github.com/casey/just)
- [uv](https://docs.astral.sh/uv/)

The Zelos guide to [developing extensions](https://docs.zeloscloud.io/latest/sdk/how-to/develop-extensions/) covers the SDK, and [Package and Publish Extensions](https://docs.zeloscloud.io/latest/sdk/how-to/package-extensions/) covers the archive and the marketplace.

## Layout

`extension.toml` describes everything the extension contributes:

| Part | Files | Manifest |
| ---- | ----- | -------- |
| Agent runtime | `main.py`, the Python package, `pyproject.toml`, `config.schema.json` | `[agent]`, `[config]` |

| Path                                 | Purpose                                                                       |
| ------------------------------------ | ----------------------------------------------------------------------------- |
| `main.py`                            | Entry point; the app, `demo`, `demo-server` and `trace` commands              |
| `zelos_extension_ads/ams.py`         | AMS NetIds, the local router, and Beckhoff's UDP identify and add-route step  |
| `zelos_extension_ads/symbols.py`     | Symbol and datatype tables flattened to traced fields; filters and symbol map |
| `zelos_extension_ads/transport.py`   | The link a client drives: pyads to a real target, or the demo PLC             |
| `zelos_extension_ads/client.py`      | One client per target, and the runner that drives them                        |
| `zelos_extension_ads/actions.py`     | The ADS actions                                                               |
| `zelos_extension_ads/autoconfig.py`  | Find TwinCAT targets for the config form's Auto-configure button              |
| `zelos_extension_ads/ethercat.py`    | EtherCAT diagnostics through the TwinCAT EtherCAT master's ADS interface      |
| `zelos_extension_ads/cli/app.py`     | Config load, `targets[]` and `advanced` resolution, startup validation        |
| `zelos_extension_ads/demo/plc.py`    | The simulated PLC, and the in-process link demo mode uses                     |
| `zelos_extension_ads/demo/server.py` | The demo PLC behind AMS/TCP                                                   |
| `tests/test_symbols.py`              | Symbol tables, flattening, names, filters, values and the map                 |
| `tests/test_client.py`               | The client against the demo PLC, in process and over AMS/TCP                  |
| `tests/test_ethercat.py`             | EtherCAT trace and actions through the demo master                            |
| `tests/test_action_flags.py`         | Each action's read-only and hidden flags                                      |
| `tests/test_hil.py`                  | Tests against a real TwinCAT target                                           |
| `tests/conftest.py`                  | The demo server fixture and the helpers that run a client                     |

## Commands

| Command                       | Description                                                |
| ----------------------------- | ---------------------------------------------------------- |
| `just install`                | Install dependencies and the pre-commit hooks              |
| `just format`                 | Format code with ruff                                      |
| `just check`                  | Check formatting and lint with ruff                        |
| `just test`                   | Run tests (pytest)                                         |
| `just package`                | Package for the Zelos marketplace                          |
| `just ci`                     | Install from the lockfile, check, test and package         |
| `just release VERSION`        | Bump version, check, test, commit, tag                     |
| `just delete-release VERSION` | Delete a GitHub release and its tag, then force-push       |
| `just clean`                  | Remove build output, caches and packages                   |
| `just run`                    | Run the extension against `./config.json`                  |
| `just demo`                   | Run against the built-in demo PLC and its EtherCAT segment |
| `just sim`                    | Run the demo PLC behind AMS/TCP on `127.0.0.1:48898`       |
| `just hil`                    | Run the real-target tests; needs `ZELOS_ADS_HOST`          |
| `just lock`                   | Update `uv.lock` after changing dependencies               |

`just install` installs the pre-commit hooks in `.pre-commit-config.yaml`. CI runs on every
push and pull request: it checks that `uv.lock` is in sync, then runs `just check` and `just test`.

## Testing

The wire tests put the demo PLC behind AMS/TCP on `127.0.0.1:48898` and drive it through pyads.
They exercise the real protocol path: uploads, sum reads, notifications and reconnection.
AdsLib connects to that port only, so the wire tests skip when something else holds it.

`tests/test_hil.py` runs against a real target and skips unless `ZELOS_ADS_HOST` is set:

| Variable           | Value                                       |
| ------------------ | ------------------------------------------- |
| `ZELOS_ADS_HOST`   | The target's host; unset skips the tests    |
| `ZELOS_ADS_NET_ID` | The target's AMS NetId; empty asks the host |
| `ZELOS_ADS_PORT`   | The PLC runtime port; defaults to `851`     |

The target must hold a route for this machine, which the `add_route` action adds.
It must run a program with `MAIN.nCycle` (UDINT, +1 per cycle), `MAIN.fSine` (LREAL), `MAIN.bOut1` and `MAIN.bOut2` (BOOL).
The tests write `MAIN.bOut1` and restore it to `FALSE`, and only read the PLC state.
The EtherCAT test reads only, and expects the master in OP with at least one slave.

```bash
ZELOS_ADS_HOST=192.168.1.10 just hil
```

## Run it locally

`just run` reads `./config.json`, and it needs at least one target. Start the demo PLC in one terminal:

```bash
just sim
```

Then run the extension in another:

```bash
echo '{"targets": [{"name": "demo", "host": "127.0.0.1", "ams_net_id": "192.168.100.10.1.1"}]}' > config.json
just run
```

`just run` connects to the agent at `ZELOS_AGENT_URL`, else `http://localhost:2300`.
`just demo` needs no config and no second terminal: it runs the demo PLC in process.

## Run it in Zelos

With `just sim` still running:

```bash
zelos extensions install-local .
zelos extensions start local.ads --config '{"targets": [{"name": "demo", "host": "127.0.0.1", "ams_net_id": "192.168.100.10.1.1"}]}'
zelos extensions logs local.ads
```

A local install runs from your project directory, so you do not install again after a code
change:

- Agent runtime: stop and start the extension. After a dependency change, stop it and run
  `zelos extensions reinstall <id>`.
- `extension.toml`: run `zelos extensions install-local .` again.

## Packaging

```bash
just package
```

The archive lands next to `extension.toml` as `<name>-<version>.tar.gz`, where `<name>` is the
manifest name in lowercase with every other run of characters turned into one hyphen. It holds
`extension.toml`, the README, the icon, the config schema and the paths listed in
`[package] paths`. With an agent runtime it also holds `pyproject.toml`, `uv.lock` and
`actions.json`, the list of actions that run while the extension is stopped.

## Release

Add the release to `CHANGELOG.md` and commit it, then:

```bash
just release 1.0.0
git push --follow-tags
```

The release workflow checks, tests and packages the extension, then attaches the archive to a
GitHub release. The Zelos marketplace picks up each new release.
