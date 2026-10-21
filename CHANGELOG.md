# Changelog

All notable changes to Zelos Beckhoff ADS Extension will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial implementation on pyads 3.6 (Beckhoff AdsLib on Linux/macOS, the TwinCAT router on Beckhoff RT Linux and Windows).
- Multiple targets in one extension (`targets[]`); one unreachable target never stalls the others. Every target must connect at start; afterwards drops are retried with capped exponential backoff (3 s doubling to 60 s).
- Symbol and datatype tables uploaded on every connect; structs, arrays (multi-dimensional, row-major) and aliases flattened to scalar fields; pointers, references and interfaces skipped. Trace layout `ADS/<target>/<POU or GVL>/<variable>` with struct members and array elements as `stX_member`, `arr_2`.
- Notifications by default (ADS device notifications, on change, checked every `cycle_time` ms), logged at the PLC's timestamp, one row per POU and timestamp; a symbol silent for `min_update_interval` is re-read. Sum-read polling (`poll`) as the alternative, and the fallback above `max_notifications` symbols or when the target refuses a notification.
- Without a symbol map, every scalar PLC variable is traced except TwinCAT's own GVLs (`TwinCAT_*`, `Constants`, `Global_Version`, `LREAL_CONST`) and Beckhoff library globals (`Tc2_*`, `Tc3_*`); `include` / `exclude` globs over the symbol path (`advanced`, per-target override).
- Symbol map files (`events` -> `symbol`, `name`, `unit`, `scale`); a scaled field is traced as Float64; a missing symbol is one ERROR per connect.
- `_target` health event per target: ADS state, device state, connected, transport, counts, clock skew, last error.
- A changed symbol table reconnects and uploads again: the symbol version (0xF008) is read with every health probe in both transports and subscribed in notification mode, and stale-address errors count too. Fields added on reconnect start a new trace segment.
- Shutdown is bounded: a link call that never returns is abandoned after 3 s and the process exits, traces flushed first.
- Actions under `ADS/`: `get_status`, `list_symbols`, `read_symbol`, `write_symbol` (by symbol handle, so the target resolves the name at write time; reads back), `plc_state` (read-only), `add_route` (Beckhoff's UDP 48899 handshake with the credentials as parameters, never stored; standalone), `auto_config` (standalone, hidden: UDP identify of the form's targets, or a broadcast for new ones).
- An empty `ams_net_id` is asked from the host (UDP identify) on every connect.
- Local AMS NetId derived from the interface toward the first target plus `.1.1` (`advanced.local_ams_net_id` overrides) on router-less platforms.
- Demo PLC: in-process for `demo` mode on every platform, and behind AMS/TCP (`demo-server`, `just sim`) for other clients and the wire tests.
- CLI: app mode, `demo`, `demo-server`, `trace <host> [map]`.

---

*For maintainers: When releasing, move items from [Unreleased] to a new version section:*

```markdown
## [0.1.0] - 2026-10-21

### Added
- (move items from Unreleased here)
```
