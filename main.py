#!/usr/bin/env python3
"""Zelos Beckhoff ADS Extension - CLI entry point.

Modes:

1. App mode (default): configuration comes from the Zelos App
2. Demo mode: built-in demo PLC, no hardware
3. CLI trace mode: explicit target and symbol map on the command line

Examples:
    uv run main.py                                          # app mode
    uv run main.py demo                                     # demo PLC
    uv run main.py demo-server                              # demo PLC on 127.0.0.1:48898
    uv run main.py trace 192.168.1.10 --ams-net-id 5.1.2.3.1.1
    uv run main.py trace 192.168.1.10 symbols.json
"""

from __future__ import annotations

import logging
import time

import rich_click as click

from zelos_extension_ads import ACTION_PREFIX as _ACTION_PREFIX
from zelos_extension_ads.cli import app as app_mode

#: Re-exported so a packaging-time action inventory - which reads this entry
#: module - sees the same namespace the live registration uses. See the
#: definition in `zelos_extension_ads/__init__.py`.
ACTION_PREFIX = _ACTION_PREFIX

# UTC ISO 8601 with ms, matching the SDK's Rust tracing lines in the same log stream
logging.Formatter.converter = time.gmtime
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03dZ %(levelname)5s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)


@click.group(invoke_without_command=True)
@click.option("--demo", is_flag=True, help="Run in demo mode with the built-in demo PLC")
@click.pass_context
def cli(ctx: click.Context, demo: bool) -> None:
    """Zelos Beckhoff ADS Extension - trace, read and write TwinCAT PLC variables.

    Without a subcommand this starts in app mode using the configuration saved
    by the Zelos App. Use --demo or the 'demo' subcommand for the demo PLC,
    and 'trace' for direct CLI access.
    """
    if ctx.invoked_subcommand is None:
        app_mode.run_app_mode(demo=demo)


@cli.command()
def demo() -> None:
    """Run against the built-in demo PLC.

    A simulated TwinCAT program (cycle counter, sine, outputs, a motor struct,
    an array, a GVL) traced in-process. No hardware required.
    """
    app_mode.run_app_mode(demo=True)


@cli.command("demo-server")
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind address")
@click.option("--port", type=int, default=48898, show_default=True, help="AMS/TCP port")
def demo_server(host: str, port: int) -> None:
    """Run the demo PLC behind AMS/TCP until Ctrl-C, for any ADS client.

    Its AMS NetId is 192.168.100.10.1.1 on AMS port 851; route it to this host.
    AdsLib clients (this extension on Linux/macOS, pyads) only connect to 48898.
    """
    import asyncio

    from zelos_extension_ads.demo.server import run_server

    try:
        asyncio.run(run_server(host, port))
    except OSError as e:
        raise click.ClickException(
            f"Could not start the demo PLC on {host}:{port} ({e}); one may already be running there"
        ) from e
    except KeyboardInterrupt:
        pass


@cli.command()
@click.argument("host", type=str)
@click.argument("symbol_map_file", type=click.Path(exists=True), required=False)
@click.option("--ams-net-id", default="", help="Target AMS NetId; default: asked from the host")
@click.option(
    "--port", "-p", type=int, default=851, show_default=True, help="AMS port of the PLC runtime"
)
@click.option("--name", default="", help="Target name (default: sanitized host)")
@click.option(
    "--cycle-time", "-c", type=int, default=100, show_default=True, help="Cycle time in ms"
)
@click.option(
    "--transport",
    type=click.Choice(["notification", "poll"]),
    default="notification",
    show_default=True,
    help="Target-pushed changes, or one sum read per cycle",
)
@click.option(
    "--timeout", type=float, default=5.0, show_default=True, help="Request timeout in seconds"
)
@click.option(
    "--local-ams-net-id", default="", help="NetId this machine presents (default: derived)"
)
@click.option("--include", multiple=True, help="Symbol glob to trace (repeatable)")
@click.option("--exclude", multiple=True, help="Symbol glob not traced (repeatable)")
@click.option("--prefix", default="ADS", show_default=True, help="Trace prefix; empty for none")
@click.option(
    "--ethercat",
    is_flag=True,
    help="Also trace the target's EtherCAT master (NetId = target NetId with .2.1)",
)
def trace(
    host: str,
    symbol_map_file: str | None,
    ams_net_id: str,
    port: int,
    name: str,
    cycle_time: int,
    transport: str,
    timeout: float,
    local_ams_net_id: str,
    include: tuple[str, ...],
    exclude: tuple[str, ...],
    prefix: str,
    ethercat: bool,
) -> None:
    """Trace one TwinCAT target from the command line.

    HOST is the target's IP or host name. SYMBOL_MAP_FILE is an optional JSON
    symbol map; without one every scalar PLC variable is traced.

    \b
    Examples:
        uv run main.py trace 192.168.1.10
        uv run main.py trace 192.168.1.10 symbols.json --ams-net-id 5.1.2.3.1.1
        uv run main.py trace localhost --include 'MAIN.**' --transport poll -c 50
    """
    logger.info("Starting ADS trace: %s", host)
    # One target through the same resolution as app mode.
    app_mode.run_config(
        {
            "targets": [
                {
                    "name": name,
                    "host": host,
                    "ams_net_id": ams_net_id,
                    "port": port,
                    "symbol_map_file": symbol_map_file or "",
                    "cycle_time": cycle_time,
                    "include": list(include),
                    "exclude": list(exclude),
                    "ethercat": ethercat,
                }
            ],
            "advanced": {
                "prefix": prefix,
                "timeout": timeout,
                "transport": transport,
                "local_ams_net_id": local_ams_net_id,
            },
        }
    )


if __name__ == "__main__":
    cli()
