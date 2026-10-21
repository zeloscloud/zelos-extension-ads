"""Demo PLC: an in-process TwinCAT-shaped program, reachable as a `Link` or over AMS/TCP."""

from zelos_extension_ads.demo.plc import DEMO_NET_ID, DemoLink, DemoPlc

__all__ = ["DEMO_NET_ID", "DemoLink", "DemoPlc"]
