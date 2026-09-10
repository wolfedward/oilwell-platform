# Task 1 — Virtual DSP+MCU terminal telemetry

## RED / GREEN evidence

`python -m unittest tests.test_simulator -v` first failed with
`ModuleNotFoundError: No module named 'backend.protocol'`.  After adding the
bounded frame protocol and deterministic terminal, the same focused suite
passes (6 tests).  A temporary-database HTTP smoke test logged in, listed 48
devices, applied a scenario, and performed a versioned channel PATCH.  A
restart smoke test retained a legitimate channel update and accepted the new
manifest projection. `py_compile` succeeds for backend modules and `server.py`.

## Frame layout

Frames are `magic(ODSP) | version(u8) | device length(u8) | sequence(u64) |
timestamp length(u8) | canonical JSON length(u16) | device id | +08:00 ISO
timestamp | canonical UTF-8 object JSON | sha256(body)`. Decoder bounds every
length before slicing and rejects bad magic/version, truncation/trailing bytes,
oversize fields, invalid identifiers/timestamps/UTF-8/JSON, noncanonical JSON,
and digest mismatch.  Omitting a timestamp uses an aware Shanghai local time.

## States and scenarios

Terminal states are booting, online, running, stopped, fault, local_control,
and offline, with explicit legal transition table.  The normal,
supply_shortage, pump_leak, empty_pump, sensor_drift, packet_loss, and offline
scenarios are deterministic for profile/seed/tick timestamp; scenarios expire
on the virtual tick clock.  Offline emits no business frames; packet loss
consumes sequences before deliberately omitting frames.  Every normal tick
emits heartbeat, telemetry (current, voltage, active power, power factor,
frequency, stroke, strokes/minute, fillage), and an electric-card frame.

## API, permissions, and persistence

`GET /api/devices`, detail, and registers require `devices.read`;
`POST .../scenarios` needs `devices.control`; versioned channel PATCH needs
`devices.configure`. Responses explicitly say `connection_mode: virtual` and
`hardware_connected: false`.  Scenario/device-event records and configuration
audits use one transaction. Auth bootstrap version 2 grants new capability
definitions once; later revocations are not recreated on restart.

## Integrity and lifecycle

The hash now uses projection `device-config-v4`: it protects device/well/serial/
type/firmware and channel/device/code/name identity but excludes device status
and mutable channel configuration/version. Exact earlier v3/v2/v1 projections
are accepted only for one CAS upgrade with audit. The server owns a 48-terminal
registry after migration/seed, starts it at `serve_forever`, and stops/joins it
inside `server_close` before the instance lock is released. Frames remain in a
bounded in-memory queue and are not written into fixed simulator history.

## Residual risk

Task 2 still owns durable raw-frame ingestion, quality-event/replay behavior,
and production persistence of live virtual frames; Task 1 deliberately exposes
only its bounded hand-off queue.

## Fresh independent verification (2026-09-10)

The current HEAD (`ec75f85`) was verified with isolated temporary database/log
environment paths and serial unittest runs. Results: `tests.test_auth` 19/19
in 60.506s; `tests.test_database` 23/23 in 12.945s;
`tests.test_health` 10/10 in 10.323s; `tests.test_seed` 11/11 in 46.183s;
`tests.test_lifecycle` 5/5 in 27.712s; and `tests.test_simulator` 6/6 in
0.005s. The `tests.test_well_api` module ran 15 tests and had one ERROR.

The isolated failing command was:

`python -m unittest tests.test_well_api.WellApiTests.test_status_write_is_audited_versioned_permissioned_and_survives_restart -v`

It failed at the replacement `create_server()` after a legitimate
`PATCH /api/wells/W-001/status` to `stopped`, with
`backend.seed.SeedDataIntegrityError: seed dataset incomplete: content
fingerprint` from `_validate_manifest`. The current `device-config-v4`
projection still includes `wells.status` (the `well_fields` branch in
`backend/seed.py`), so a mutable lifecycle status change invalidates the
manifest on restart. This is a production-contract failure and was not
modified during verification.

A separate static contract issue was also observed in
`backend/services/devices.py`: `apply_scenario()` mutates the in-memory
terminal before obtaining `before = self._scenario(device_id)`. Consequently
the scenario audit's `before` snapshot can already contain the newly applied
state, and a later database transaction failure would leave the registry
mutation in place. No production change was made; this is recorded for the
implementer.

Registry/lifecycle smoke verification created a temporary seeded server and
confirmed 48 terminals, a live runner after `serve_forever`, a stopped HTTP
thread after shutdown, `RUNNER_ALIVE_AFTER_CLOSE=False`, and no non-daemon
task threads. At the end of all verification commands, no `python` or
`pythonw` processes remained; temporary verification directories were deleted.

## Fix round 1

- `device-config-v4` and all later `device-config-vN` projections now retain
  the well identity/binding fields but exclude mutable `wells.status` and
  `version`. The exact `ec75f85` v4 fingerprint remains a one-time audited CAS
  migration candidate. A legitimate well status change survives restart, while
  an immutable well identity edit remains fail-closed.
- Scenario application now serializes with the registry runner, captures its
  real before-image before any mutation, builds a pure immutable plan, writes
  device/scenario/event/audit records in one transaction, and applies that
  prevalidated plan only after commit. The plan carries its terminal state, so
  persisted `devices.status` and the in-memory terminal cannot diverge through
  duplicated status logic. Audit/constraint failures leave both stores
  unchanged; concurrent requests form one before/after chain.
- TDD evidence: the new terminal-plan regression was RED with
  `AttributeError: ScenarioState has no attribute terminal_state`; after the
  minimal plan-state change it passed. Seed v4-restart/identity-tamper,
  scenario-atomicity, simulator, well API, auth, lifecycle, database, and
  health modules passed. Final `python -m unittest discover -s tests -v` ran
  98 tests successfully in 233.068 seconds; `py_compile` and `git diff --check`
  also succeeded.

## Fix round 2

- Frame JSON now has one bounded canonicalization boundary: encode and decode
  turn non-finite numbers, lone surrogates, recursive values, and over-deep
  valid JSON into `FrameError` subclasses. Decoder constant parsing rejects
  `NaN`/`Infinity` before canonical comparison; nesting is limited to 64 while
  respecting quoted/escaped bytes.
- Scenario persistence now records `duration_minutes`, restores active terminal
  plans from the database with an injectable clock, and normalizes stale or
  invalid plans transactionally. Runtime expiry persists normal state, event,
  and audit before committing terminal memory; a database failure leaves that
  terminal unchanged and skips its tick.
- The live queue is private and independently locked. `drain_frames`, a single
  sink, and `frame_stats` provide atomic consumption, success delivery, failure
  fallback, and accurate bounded-overflow accounting. Sink callbacks happen
  outside the registry/scenario lock.
- RED evidence: malicious signed JSON leaked `ValueError` and
  `UnicodeEncodeError`; deep objects were accepted; registry construction
  rejected `clock`; non-object scenario POST raised `AttributeError`; and the
  startup-expiry audit captured `normal` rather than the persisted `offline`
  before-image. Each focused regression passed after the corresponding minimal
  repair. Real HTTP tests confirm missing/unknown scenario fields return 422
  without state change. Final discovery ran 107 tests successfully in 256.484
  seconds.

## Fix round 3

- Scenario expiry now acquires transition ownership with a conditional update
  over the persisted name, duration, and expiry tuple. Only the CAS winner
  changes device status and writes the expiry event/audit; a losing registry
  reloads the current persisted scenario. Concurrent startup and runtime
  expiry therefore produce one business transition.
- An active pre-duration scenario is no longer ended during upgrade. A future
  expiry with legacy duration `0` derives the bounded remaining minutes,
  migrates that value with an audited CAS, and restores the terminal. Competing
  initializers write the migration audit once and converge on the same state.
- The simulator lifecycle thread no longer invokes arbitrary callbacks. Its
  optional sink accepts only an exact bounded standard-library `queue.Queue`
  and uses `put_nowait`; a full mailbox falls back to the private bounded
  registry queue. Callable, unbounded, and subclassed sinks are rejected, so
  external code cannot block `stop()`.
- RED evidence came from independent reproductions: two registries wrote two
  expiry events, an upgraded active offline scenario became normal/online, and
  a blocking callback left the non-daemon runner alive after stop. The focused
  scenario/simulator suites now pass 23/23 in 39.614 seconds. A fresh full
  `python -m unittest discover -s tests -v` run passes 110/110 in 281.107
  seconds with no residual Python process.
