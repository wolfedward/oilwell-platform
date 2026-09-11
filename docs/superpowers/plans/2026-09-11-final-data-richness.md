# Final Demonstration Richness Plan

**Goal:** Deliver a coherent, restart-safe demonstration build by 15:30 with correlated dashboard trends, complete strategy/recovery evidence, a full manual-review condition catalog including custom entries, and clean hover-only chart points.

## Acceptance contract

- Existing `data/platform.db` is upgraded or rendered completely after restart; success must not depend on deleting the database.
- Fluid and energy trends fluctuate visibly, are positively correlated, and their latest points equal the displayed latest-day summary.
- Manual review exposes the 20 supported operating conditions plus a validated custom condition; saving a review preserves the immutable machine result and records reviewer/version/audit evidence.
- Strategy detail and recovery analysis show coherent identifiers, versions, timestamps, cycle values, forecast values, approval records, observations, fitted curve, and fit metrics without placeholder dashes.
- Electric-power and dynagraph charts render continuous lines by default; every sample remains keyboard/mouse accessible, with only the active point, coordinates, and crosshair visible on interaction.

## Workstream 1 — Dashboard trends

- [ ] Add a failing contract test for non-flat variation, positive fluid/energy correlation, and latest-point/summary equality.
- [ ] Implement deterministic day-level variation at the service/data reconciliation boundary without random request-time noise.
- [ ] Verify dashboard API and page labels against the same units and date range.

## Workstream 2 — Manual diagnosis review

- [ ] Add failing service/API/page tests for the 20-condition catalog and custom-condition validation/persistence.
- [ ] Implement the catalog, custom input interaction, immutable machine-result handling, and audit-safe review storage.
- [ ] Verify standard and custom review submissions and history rendering.

## Workstream 3 — Strategy and recovery evidence

- [ ] Add failing tests that reject blank strategy, approval, tracking, recovery, model, and observation fields.
- [ ] Backfill or derive coherent evidence for existing seeded strategy records and recovery tests.
- [ ] Verify strategy detail and recovery analysis pages against live API data after restart.

## Workstream 4 — Curve interaction

- [ ] Add failing browser/style tests proving sample markers are invisible at rest and visible on hover/focus.
- [ ] Preserve transparent hit targets and unified tooltips/crosshairs while keeping raw, filtered, inferred, and measured lines unobstructed.
- [ ] Verify both mouse and keyboard interaction in Edge.

## Integration

- [ ] Review every workstream diff for file overlap and data consistency.
- [ ] Run focused tests, then full suite, JavaScript syntax checks, `git diff --check`, restart the service, and execute the live acceptance smoke checks.
- [ ] Commit only project files; do not touch or stage the user-owned `scripts/` directory.
