# Dynagraph Comparison Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add administrator permission union, twenty traceable card profiles, dual inferred/sensor dynagraph comparison, and visible point tooltips to every two-dimensional chart.

**Architecture:** Extend the existing electric-card payload with deterministic profile and sensor point fields, persist them through the current raw-message path, and store comparison outputs in existing analysis JSON snapshots. Add a reusable delegated chart-interaction binder and enrich the existing dynagraph pages without introducing a new framework or network dependency.

**Tech Stack:** Python 3.13 standard library, SQLite, native ES modules and SVG, `unittest`.

**Spec:** `docs/superpowers/specs/2026-09-11-dynagraph-comparison-design.md`

## Global Constraints

- Deliver a demonstrable version by 2026-09-11 15:00 Asia/Shanghai.
- Keep the service offline on `127.0.0.1`; add no dependency.
- Preserve raw electric and sensor point data, model version and provenance.
- Show profile labels separately from machine diagnosis labels.
- Do not modify or stage the user-owned untracked `scripts/` directory.
- Use RED→GREEN tests for every behavior change.

---

### Task 1: Administrator Permission Union

**Files:** Modify `backend/seed.py`; test `tests/test_auth.py` and `tests/test_seed.py`.

**Interfaces:** `ROLE_PERMISSIONS["administrator"]` equals the union of every role plus `users.manage`, `roles.manage`, `audit.read`; bootstrap reconciles it idempotently.

- [ ] Add a failing test comparing the administrator's effective API permission set with the literal union of all role permission rows.
- [ ] Add a restart test proving a removed administrator business permission is restored while another role is unchanged.
- [ ] Derive the administrator tuple after declaring business roles, bump `AUTH_BOOTSTRAP_VERSION`, and insert every missing administrator grant during bootstrap.
- [ ] Run `python -m unittest tests.test_auth tests.test_seed` and commit `feat: grant administrator all role permissions`.

### Task 2: Twenty Profiles and Dual-Card Analysis

**Files:** Modify `backend/algorithms/dynagraph_profiles.py`, `backend/simulator.py`, `backend/services/acquisition.py`, `backend/services/analysis.py`; test `tests/test_dynagraph_profiles.py`, `tests/test_simulator.py`, `tests/test_analysis_api.py`.

**Interfaces:** `PROFILE_DEFINITIONS` contains the 20 stable codes and labels; `build_profile_card()` returns `profile_code`, `profile_label`, `sensor_samples`; `compare_dynagraphs(inferred, measured)` returns `mae_kn`, `max_error_kn`, `rmse_kn`, `area_error_percent`, `stroke_error_m`, `correlation`, `upstroke_rmse_kn`, `downstroke_rmse_kn`.

- [ ] Add failing literal tests for exactly 20 codes, distinct rounded feature vectors, normal load difference `20..35 kN`, legal sensor samples and deterministic output.
- [ ] Add failing tests proving condition-specific electric-card power signatures are distinct and remain coherent with each inferred dynagraph.
- [ ] Add failing analysis test that persists both point sets and eight finite comparison metrics in detail/model-run payloads.
- [ ] Implement condition-specific upper/lower branch modifiers, sensor error generation and strict payload validation.
- [ ] Align sensor points by phase, calculate comparison metrics, and persist them inside existing JSON snapshots.
- [ ] Run targeted algorithm/simulator/API tests and commit `feat: add dual dynagraph comparison profiles`.

### Task 3: Unified Visible Chart Tooltips

**Files:** Modify `frontend/charts.js`, `frontend/pages/dynagraph-analysis.js`, `app.js`, `styles.css`; test `tests/test_analysis_pages.py`, `tests/test_page_contracts.py`.

**Interfaces:** every SVG point has `data-chart-point`, `data-series`, `data-x`, `data-y`, `data-x-unit`, `data-y-unit`; `bindChartInteractions(root)` creates one bounded `.chart-tooltip` and highlight guides using delegated pointer/focus events.

- [ ] Add failing output tests for line, electric raw/filter and dynagraph point metadata plus escaped visible tooltip values.
- [ ] Emit the unified point attributes and focusable hit targets from both chart builders.
- [ ] Implement delegated tooltip/highlight behavior and call it once after every route renderer.
- [ ] Add bounded tooltip, guide and focus CSS; run page tests and JS syntax checks; commit `feat: add point tooltips to all xy charts`.

### Task 4: Dual-Card Controls and Operational Cards

**Files:** Modify `frontend/pages/dynagraph-analysis.js`, `frontend/pages/dynagraphs.js`, `styles.css`; test `tests/test_analysis_pages.py`, `tests/test_page_contracts.py`.

**Interfaces:** consumes `sensor_points`, `comparison`, `parameters`, `profile_code/profile_label`; produces two series sharing one dynagraph domain and checkboxes `toggleInferred`/`toggleMeasured`.

- [ ] Add failing page tests for two checked toggles, distinct legends, profile versus diagnosis labels, comparison metrics and surrounding engineering parameters.
- [ ] Render the two series on one domain, bind checkboxes to hide either series, and retain per-point tooltips.
- [ ] Enrich archive cards with status/alarm badges and all requested parameters/actions using API fields only.
- [ ] Run page/flow tests and JS checks; commit `feat: add dual dynagraph operations workspace`.

### Task 5: Live Database Repair and Verification

**Files:** Modify `docs/验收验证记录.md` only after fresh evidence exists.

- [ ] Run Python compile and JavaScript syntax checks.
- [ ] Run focused algorithm/auth/API/page regression tests.
- [ ] Restart the platform; analyze one normal and one fault profile; assert normal load difference `20..35 kN` and dual comparison fields.
- [ ] Run `python acceptance_smoke.py`, visually inspect dashboard/dynagraph archive/analysis, and record exact evidence.
