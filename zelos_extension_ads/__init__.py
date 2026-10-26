"""Zelos Beckhoff ADS Extension - TwinCAT ADS support for Zelos.

This extension provides:
- ADS client per TwinCAT target: symbol upload, device notifications or sum-read polling
- Optional symbol map for naming and grouping; without one, every scalar PLC variable
- Zelos SDK integration for trace events and actions (read, write, PLC state, routes)
- Demo PLC for testing without hardware
"""

#: Action namespace, live and in the packaged inventory; matches `name` in `extension.toml`.
ACTION_PREFIX = "ADS"
