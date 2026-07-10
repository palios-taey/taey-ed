# Future App Release: BT Telemetry

Status: future app rebuild decision only.

Observed in cl2-primitives on 2026-07-10: the frozen Mac bundle does not
round-trip BT blackboard snapshots or stored `find_all` results in
`last_result`. The server-side cl2 primitive therefore uses only successive
posted AX-tree snapshots for wait-until-stable and scoped exact addressing.

Deferred telemetry-dependent variants:

- `bt_blackboard` snapshots for per-flow sentinel checks.
- `bt_find_all_results` snapshots for empty/non-empty option stability checks.
- CL3 execution beacons that depend on blackboard state.

CL3 consequence: execution beacons must use tree-observable screen state
changes in the current plan. Blackboard-only sentinels require a Jesse-approved
Mac app rebuild and must not be shipped as server assumptions.
