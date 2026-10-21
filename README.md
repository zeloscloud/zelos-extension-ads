# Zelos Beckhoff ADS

A Zelos extension for Beckhoff TwinCAT ADS. Trace PLC variables from any TwinCAT 2 or 3 runtime by device notification or polling, and read, write and inspect symbols from the Zelos App. No TwinCAT function license needed: ADS is built into every runtime.

## Features

- 📡 **Notifications or polling**: The PLC pushes value changes with its own timestamps; or one sum read per cycle
- 🔍 **No map needed**: The symbol table is uploaded on every connect and every scalar variable traced, structs and arrays flattened
- 📄 **Symbol map files**: Group and name symbols, set units and scaling in a simple JSON file
- 🏭 **Multiple targets**: Any number in one extension; one unreachable target never stalls the others
- ✏️ **Read, write & state actions**: Interactive symbol access and the PLC's run state from the Zelos App
- 🔑 **Routes from the app**: Add this machine's route to a target with its credentials, kept nowhere
- 🧪 **Demo mode**: Built-in demo PLC for testing without hardware

## Quick Start

From the CLI, on the agent that can reach the target:

```bash
zelos extensions install zeloscloud/zelos-extension-ads
zelos extensions start zeloscloud.zelos-extension-ads \
  --config '{"targets": [{"host": "192.168.1.10"}]}'
```

In the app:

1. **Install** the extension from the Zelos App
2. **Configure** your targets (host, and a symbol map file or nothing: every variable), or press Auto-configure
3. **Add a route** on each target for this machine (`add_route` action; not needed on a Beckhoff device or a Windows host with TwinCAT)
4. **Start** the extension to begin streaming data
5. **View** real-time PLC variables in your Zelos App

## Configuration

### Targets

| Setting | Default | Description |
|---------|---------|-------------|
| Name | host | Trace name (`cx9240`); unique |
| Host | `localhost` | IP or host name of the TwinCAT router |
| AMS NetId | | Target NetId (`5.1.2.3.1.1`); empty asks the host (UDP 48899) on every connect |
| AMS Port | `851` | PLC runtime: 851 first TwinCAT 3 PLC, 852 second, 801 TwinCAT 2 |
| Symbol Map File | | JSON [symbol map](#symbol-map); empty = [every variable](#every-variable) |
| Cycle Time | `100` ms | Change check period (notification) or poll period |
| Transport | `notification` | `notification` (PLC pushes changes) or `poll` |
| Include / Exclude | | Symbol globs (`MAIN.**`, `**.bDebug`) |

### Advanced

Applies to every target unless the target overrides it.

| Setting | Default | Description |
|---------|---------|-------------|
| `prefix` | `ADS` | Trace source: `ADS/cx9240/MAIN`; cleared = one source per target |
| `timeout` | `5.0` | Request timeout (s) |
| `log_level` | `INFO` | Logging verbosity |
| `local_ams_net_id` | derived | NetId this machine presents (Linux, macOS); empty = local IP + `.1.1` |
| `transport` | `notification` | Default transport |
| `max_notifications` | `1000` | Symbols per target above which it polls |
| `min_update_interval` | `60` | Re-read a subscribed symbol silent this long (s) |
| `include` / `exclude` | | Symbol globs; TwinCAT's own GVLs are excluded unless included |

Every target must connect at start, or the extension stops with an error naming it. Once running, a dropped link is retried with backoff (3 s doubling to 60 s), the symbol table uploaded again and the trace resumed.

### Routes

TwinCAT accepts a client only from an AMS NetId it holds a route for.

| Where the extension runs | What to do |
|---|---|
| A Beckhoff device (TwinCAT/BSD, RT Linux) tracing itself | Nothing: its own router carries the connection |
| Linux or macOS | Once per target: `add_route` action with the target's host and a TwinCAT user. The route names this machine's NetId (`local_ams_net_id`, default local IP + `.1.1`). Or add it in TwinCAT XAE |
| Windows with TwinCAT, or a Beckhoff device tracing another | Add the route in that router (XAE, `adstool addroute`); this extension uses it |

Plain ADS (TCP 48898) carries no encryption or authentication beyond the route: keep it on a trusted network.

### Every variable

A target without a symbol map traces every scalar PLC variable it holds, read-only: the event is the POU or GVL (`MAIN`, `GVL_IO`), the field the variable, struct members and array elements flattened (`stMotor_fSpeed`, `arrTemps_2`). `TwinCAT_*`, `Constants`, `Global_Version` and `LREAL_CONST` are left out unless `include` names them.

### Auto-configure

The config form's Auto-configure button asks each target in the form for its AMS NetId, or with none, finds TwinCAT routers on this machine and its network (UDP 48899 broadcast) and adds one target each on AMS port 851.

## Symbol Map

```json
{
  "events": {
    "motor": [
      {"symbol": "MAIN.stMotor.fSpeed", "name": "speed", "unit": "rpm"},
      {"symbol": "MAIN.stMotor.bRunning", "name": "running"}
    ],
    "process": [
      {"symbol": "GVL_IO.nPressure", "name": "pressure", "unit": "mbar", "scale": 0.1}
    ]
  }
}
```

Event keys become trace events, symbols their fields. Types and addresses come from the target.

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `symbol` | Yes | | PLC path: `MAIN.var`, `MAIN.stX.member`, `GVL.arr[2]` (case-insensitive) |
| `name` | No | the variable | Field name |
| `unit` | No | | Display unit |
| `scale` | No | `1.0` | Read value x scale; a scaled field is traced as Float64 |

## Actions

| Action | Description |
|--------|-------------|
| `get_status` | Connection state, transport, counters, PLC state |
| `list_symbols` | Every scalar on the target and where it is traced; `pattern` filters |
| `read_symbol` / `write_symbol` | Read or write one scalar by PLC name (write reads back) |
| `plc_state` | ADS state (RUN, STOP, CONFIG...) and runtime version; read-only |
| `add_route` | Add this machine's route to a target with a TwinCAT user name and password |
| `auto_config` | Find targets for the config form |

With several targets, pass `target` to pick one.

```bash
zelos actions execute "Beckhoff ADS/read_symbol" --params '{"symbol":"MAIN.nCycle","target":"cx9240"}'
zelos actions execute "Beckhoff ADS/add_route" --params '{"host":"192.168.1.10","username":"Administrator","password":"..."}'
```

## Development

```bash
just install      # deps + pre-commit hooks
just format       # ruff format + fix
just check        # ruff lint + format check
just test         # pytest (the wire tests need 127.0.0.1:48898 free)
just hil          # against a real target: ZELOS_ADS_HOST=<ip> just hil
just dev          # run app mode locally
just demo         # built-in demo PLC
just sim          # demo PLC on 127.0.0.1:48898 for any ADS client
```

## Links

- [Zelos Documentation](https://docs.zeloscloud.io)
- [SDK Guide](https://docs.zeloscloud.io/sdk)
- [pyads Documentation](https://pyads.readthedocs.io/)
- [Beckhoff ADS Introduction](https://infosys.beckhoff.com/content/1033/tc3_ads_intro/index.html)
- [GitHub Issues](https://github.com/zeloscloud/zelos-extension-ads/issues)

## CLI Usage

The extension includes a command-line interface for tracing without the Zelos App. No installation required, just use `uv run`:

```bash
uv run main.py                                                   # app mode (Zelos App config)
uv run main.py demo                                              # built-in demo PLC
uv run main.py trace 192.168.1.10                                # every variable, NetId asked from the host
uv run main.py trace 192.168.1.10 symbols.json --ams-net-id 5.1.2.3.1.1
uv run main.py trace localhost --include 'MAIN.**' --transport poll -c 50
```

## Support

For help and support:
- 📖 [Zelos Documentation](https://docs.zeloscloud.io)
- 🐛 [GitHub Issues](https://github.com/zeloscloud/zelos-extension-ads/issues)
- 📧 help@zeloscloud.io

## License

MIT License - see [LICENSE](LICENSE) for details.

---

**Built with [Zelos](https://zeloscloud.io)**
