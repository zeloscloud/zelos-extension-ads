# Contributing

## Development Workflow

```bash
just install  # dependencies + pre-commit hooks
just dev      # run app mode locally
just demo     # run against the built-in demo PLC
just test     # pytest
just check    # ruff lint + format check
just format   # auto-fix
```

1. Make your changes
2. `just format`
3. `just test`
4. Commit (pre-commit hooks run ruff automatically)

## Project Structure

```
zelos-extension-ads/
├── extension.toml                  # Extension manifest (name, version, host, package paths)
├── config.schema.json              # Configuration UI schema
├── main.py                         # Click entry point
├── pyproject.toml                  # Dependencies, ruff and pytest config
├── uv.lock                         # Locked dependency versions
├── Justfile                        # Development commands
├── LICENSE
├── README.md                       # User documentation
├── CHANGELOG.md
├── CLAUDE.md                       # Architecture notes
├── CONTRIBUTING.md                 # This file
├── .pre-commit-config.yaml
├── zelos_extension_ads/
│   ├── __init__.py                 # ACTION_PREFIX, package exports
│   ├── ams.py                      # NetIds, routers, UDP identify / add route
│   ├── symbols.py                  # Symbol tables, flattening, names, values, map
│   ├── transport.py                # Link protocol, pyads link, error classes
│   ├── client.py                   # Connection, transports, reconnect, shutdown
│   ├── actions.py                  # Action functions + register_actions()
│   ├── autoconfig.py               # Auto-configure hook
│   ├── cli/
│   │   ├── __init__.py
│   │   └── app.py                  # Config load, startup validation, serve()
│   └── demo/
│       ├── __init__.py
│       ├── plc.py                  # Demo PLC and in-process link
│       └── server.py               # Demo PLC behind AMS/TCP
├── tests/
│   ├── conftest.py                 # Demo server fixture, running() helper
│   ├── test_symbols.py             # Tables, names, filters, values, map
│   ├── test_client.py              # Client logic and wire tests
│   └── test_hil.py                 # Real target (ZELOS_ADS_HOST)
├── assets/
│   └── icon.svg                    # Marketplace icon
├── .github/
│   ├── workflows/
│   │   ├── CI.yml
│   │   └── release.yml
│   └── dependabot.yml
└── .vscode/
```

## Common Tasks

### Add a Dependency

```bash
uv add package-name        # runtime
uv add --dev package-name  # dev
```

### Package for the Marketplace

```bash
just package   # zelos extensions package .
```

Produces a `.tar.gz` for the Zelos Marketplace. CI does this automatically.

### Create a Release

```bash
just release 1.0.0
git push --follow-tags
```

## Testing

The wire tests put the demo PLC behind AMS/TCP on 127.0.0.1:48898 and drive it
through pyads, so they exercise the real protocol path (uploads, sum reads,
notifications, reconnection). They are skipped when that port is taken.

```bash
just test                        # everything
uv run pytest -v                 # verbose
uv run pytest -k test_name       # one test
ZELOS_ADS_HOST=192.168.1.10 just hil   # a real target; writes MAIN.bOut1 and restores it
```

Keep tests targeted. Prefer one integration test that proves a behavior end to
end over several unit tests that assert on internals.

## Code Quality

- ruff, line length 100, google docstrings
- Type hints on every function signature
- Comments state the constraint or the failure prevented, not what the code does

## Getting Help

- [Zelos Docs](https://docs.zeloscloud.io)
- [SDK Guide](https://docs.zeloscloud.io/sdk)
- [GitHub Issues](https://github.com/zeloscloud/zelos-extension-ads/issues)

## License

MIT - see [LICENSE](LICENSE)
