"""Pins each action's flags, so a new action or a changed flag is a deliberate choice."""

from __future__ import annotations

import inspect

from zelos_extension_ads import actions


def test_every_action_has_its_flags():
    actual = {
        name: (obj._action.read_only, obj._action.hidden)
        for name, obj in inspect.getmembers(actions, inspect.isfunction)
        if not name.startswith("_") and hasattr(obj, "_action")
    }
    assert actual == {
        "get_status": (True, False),
        "list_symbols": (True, False),
        "plc_state": (True, False),
        "read_symbol": (True, False),
        "write_symbol": (False, False),
        "ethercat_slaves": (True, False),
        "ethercat_read_sdo": (True, False),
        "add_route": (False, False),
        "auto_config": (True, True),
    }
