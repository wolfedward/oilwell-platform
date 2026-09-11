# Acceptance Richness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a richer, acceptance-ready dashboard and a traceable electric-card-to-dynagraph workflow whose charts and data look physically coherent across the 48 test wells.

**Architecture:** Keep the existing offline Python/SQLite/vanilla-JS architecture. A pure backend profile builder generates target load-displacement shapes and reverses the existing torque equation into electric power; the current converter remains the authoritative forward path. Frontend pages consume only persisted/API data and add axis-complete SVG charts and operational summaries.

**Tech Stack:** Python 3.13 standard library, SQLite, native ES modules, SVG, `unittest`.

**Spec:** `docs/superpowers/specs/2026-09-11-acceptance-richness-design.md`

## Global Constraints

- Complete a stable demonstrable version within the user's 75-minute timebox.
- Do not display “演示数据” or “验收数据” labels; retain `source_type` and timestamps for traceability.
- Do not use `Math.random`, frontend counters, or client interpolation as business data.
- Electric power and dynagraph points must share one physical source and the existing conversion equation.
- Every SVG trend/dynagraph must expose axes, ticks, grid and units.
- Add no network dependency and keep the service on `127.0.0.1`.
- Preserve the user-owned untracked `scripts/` directory.

---

### Task 1: Physically Coherent Electric-Card Profiles

**Files:**
- Create: `backend/algorithms/dynagraph_profiles.py`
- Modify: `backend/simulator.py`
- Modify: `backend/seed.py`
- Test: `tests/test_dynagraph_profiles.py`
- Test: `tests/test_simulator.py`
- Test: `tests/test_seed.py`

**Interfaces:**
- Produces: `build_profile_card(condition: str, mechanism: dict, *, variation: float = 0.0, point_count: int = 120) -> dict` returning `{kind, electric_samples, mechanism, profile}`.
- Produces: `profile_condition(device_id: str) -> str`, a stable six-shape allocation using `normal`, `supply_shortage`, `intermittent`, `pump_leak`, `empty_pump`, and `rod_break` visual profiles while preserving the existing diagnostic codes.
- Consumes: the exact force/torque direction used by `convert_electric_card`.

- [ ] **Step 1: Write failing profile tests**

Add literal assertions that each profile returns 120 strictly increasing degree samples; conversion produces a closed `0..stroke_m` curve with `25..90 kN` engineering loads; normal, shortage, intermittent, leak, empty-pump and rod-break feature vectors are distinct; and at least leak versus normal is not a constant power scaling.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest tests.test_dynagraph_profiles`

Expected: import failure because `backend.algorithms.dynagraph_profiles` does not exist.

- [ ] **Step 3: Implement the inverse profile builder**

Use `u=(1-cos(theta))/2`, `r=stroke_m/2`, a `32..42 kN` static load and `22..32 kN` useful span. Build upper/lower branches with deterministic condition modifiers, then compute:

```python
net_torque_nm = (target_load_kn - static_load_kn) * 1000.0 * r * math.sin(theta)
balance_torque_nm = balance_nm * math.sin(theta + balance_phase)
power_kw = (net_torque_nm + balance_torque_nm) * omega_motor / (
    1000.0 * gear_ratio * transmission_efficiency * motor_efficiency
)
```

Use deterministic low-amplitude harmonic variation, never global random state. Return current consistent with `P≈sqrt(3)*U*I*pf/1000`.

- [ ] **Step 4: Integrate simulator and seed payloads**

Make `VirtualTerminal.tick()` pass its active scenario/assigned profile to the builder. Replace `_dynagraph_sample()` with the same legal electric-card payload so initial `DG-*` records can run through `AnalysisService` rather than becoming eight-point load-only arrays. Keep scenario fault injection semantics intact.

- [ ] **Step 5: Run targeted tests and commit**

Run: `python -m unittest tests.test_dynagraph_profiles tests.test_simulator tests.test_seed tests.test_analysis_api`

Commit: `feat: generate coherent electric and dynagraph profiles`

### Task 2: Axis-Complete Dynagraph Operations UI

**Files:**
- Modify: `frontend/charts.js`
- Modify: `frontend/pages/dynagraphs.js`
- Modify: `frontend/pages/dynagraph-analysis.js`
- Modify: `app.js`
- Modify: `styles.css`
- Test: `tests/test_analysis_pages.py`
- Test: `tests/test_page_contracts.py`

**Interfaces:**
- Consumes: existing detail payload fields `sample_data.electric_samples`, `conversion.points`, `features`, `latest_diagnosis`, `latest_production` and model trace data.
- Produces: `charts.dynagraph(samples, {title, xLabel, yLabel, reference})` with safe finite domains, five grid/tick divisions, units and point tooltips.

- [ ] **Step 1: Write failing page contract tests**

Assert rendered chart output contains `位移 (m)`, `载荷 (kN)`, five x/y tick groups and no non-finite SVG values. Assert the analysis page contains the visible chain `电功图 → 转矩换算 → 示功图 → 智能诊断 → 产液计量 → 人工复核` and a three-stage liquid calculation summary.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest tests.test_analysis_pages tests.test_page_contracts`

Expected: missing axis labels and workflow summary assertions fail.

- [ ] **Step 3: Upgrade SVG charts**

Give dynagraph and electric-power charts plot margins, x/y axes, five labeled divisions, dashed grid, axis titles and `<title>` tooltips. Keep the existing data arrays authoritative and guard empty/constant/non-finite domains.

- [ ] **Step 4: Enrich the operations pages**

Add archive summary cards, explicit acquisition-to-review workflow steps, condition/status labels and the production flow `机构参数 → 有效冲程与充满度 → 日产液估算`. Keep existing permission-gated analyze/review actions.

- [ ] **Step 5: Run targeted tests and commit**

Run: `python -m unittest tests.test_analysis_pages tests.test_page_contracts tests.test_acceptance_flow`

Run: `node --check frontend/charts.js` and the two modified page modules.

Commit: `feat: enrich dynagraph operations and chart axes`

### Task 3: Rich Live-Backed Operations Dashboard

**Files:**
- Modify: `frontend/pages/dashboard.js`
- Modify: `styles.css`
- Test: `tests/test_page_contracts.py`
- Test: `tests/test_well_api.py`

**Interfaces:**
- Consumes: current `/api/dashboard` fields including `data_timestamp`, four-key `trends`, `production`, `quality`, `alarms`, `strategies`, `devices` and `energy`.
- Produces: a three-layer overview with running situation, 14-day production/energy trends, exception workbench, energy results and six business shortcuts.

- [ ] **Step 1: Write failing dashboard page tests**

Assert the page exposes daily fluid, oil, energy and pump-efficiency trends; alarm, quality and approval queues; a data timestamp; and contains neither `Math.random` nor a client-generated business metric.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest tests.test_page_contracts tests.test_well_api`

Expected: missing multi-metric dashboard content assertions fail.

- [ ] **Step 3: Implement the richer overview**

Render eight drill-down cards, daily-fluid and energy SVG trends from `data.trends`, production and energy result panels, a three-item exception workbench and shortcuts for acquisition, dynagraph, data quality, strategy, work orders and reports. Label day-level trends as production-daily values and current state with the server `data_timestamp`.

- [ ] **Step 4: Run targeted tests and commit**

Run: `python -m unittest tests.test_page_contracts tests.test_well_api tests.test_energy tests.test_production`

Run: `node --check frontend/pages/dashboard.js`.

Commit: `feat: enrich live-backed operations dashboard`

### Task 4: Integration, Live Data Materialization and Acceptance Smoke

**Files:**
- Modify if needed: `docs/验收操作手册.md`
- Modify if needed: `docs/验收验证记录.md`

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces: restarted local service, newly materialized profile cards, verified browser routes and a timestamped `logs/acceptance-smoke-*.json`.

- [ ] **Step 1: Run static verification**

Run: `python -m compileall backend tests` and `node --check` for every JavaScript source.

- [ ] **Step 2: Run focused regression**

Run: `python -m unittest tests.test_dynagraph_profiles tests.test_dynagraph tests.test_diagnosis tests.test_simulator tests.test_analysis_api tests.test_analysis_pages tests.test_page_contracts tests.test_well_api tests.test_acceptance_center tests.test_acceptance_flow`.

- [ ] **Step 3: Restart and materialize current data**

Use `stop-platform.cmd` then `start-platform.cmd`; wait for `/api/health`. Confirm the simulator advances `data_timestamp` and a new dynagraph can be analyzed by `algorithm_engineer`.

- [ ] **Step 4: Run smoke and browser checks**

Run: `python acceptance_smoke.py`. Inspect `#/dashboard`, `#/dynagraphs` and one `#/dynagraph-analysis?id=...` route at desktop width, checking axes, labels, data times and absence of error/loading residue.

- [ ] **Step 5: Record verification and commit**

Append exact commands, counts and smoke artifact path to `docs/验收验证记录.md`.

Commit: `docs: record richness acceptance verification`

