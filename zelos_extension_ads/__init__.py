"""Zelos Beckhoff ADS Extension - TwinCAT ADS support for Zelos.

This extension provides:
- ADS client per TwinCAT target: symbol upload, device notifications or sum-read polling
- Optional symbol map for naming and grouping; without one, every scalar PLC variable
- Zelos SDK integration for trace events and actions (read, write, PLC state, routes)
- Demo PLC for testing without hardware
"""

from zelos_extension_ads.client import ADSClient
from zelos_extension_ads.symbols import Field, SymbolMap

#: Action namespace for this extension. Single source for both surfaces - the
#: live registration (`zelos_sdk.init(name=ACTION_PREFIX)`) and the at-rest
#: inventory the packaging step dumps from `main.py`, which re-exports this.
#: A mismatch would silently produce two unrelated action trees.
#:
#: Matches `name` in `extension.toml`, which is what a user sees in the
#: extension list, so the address they read there is the address they type.
ACTION_PREFIX = "ADS"

__all__ = [
    "ACTION_PREFIX",
    "ADSClient",
    "Field",
    "SymbolMap",
]

__version__ = "0.1.0"
