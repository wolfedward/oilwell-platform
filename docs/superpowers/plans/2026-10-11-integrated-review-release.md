# Integrated review release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish remaining locally implementable annual-task software, then deploy an isolated review release without changing the frozen demo or pretending field validation.

**Architecture:** Preserve PG production acquisition/inversion modules. Add bounded auditable engineering studies using the existing identity SQLite transaction boundary; pure numerical engines have no device/network side effects. Separate explicit deployment initialization from startup.

**Tech Stack:** Python 3.13 standard library, existing psycopg/PostgreSQL 18, vanilla JavaScript/SVG, existing Windows/Edge test tools.

**Spec:** `docs/superpowers/specs/2026-10-11-integrated-review-release-design.md`

## Global Constraints

- 原工作目录、冻结演示目录/ZIP/tag、既有业务库与测试库不修改；开发仅在当前独立工作区。
- 本机部署仅绑定 `127.0.0.1`，审查 HTTP 首选 `8770`，新 PostgreSQL 首选 `55432`；端口占用则拒绝，不结束未知进程。
- 无实际设备控制：`control_enabled=false`；不新增 Modbus 写寄存器或 MQTT 下行执行，不以网页/网络代码代替硬件保护。
- 新数据保留明确来源、单位、时间、质量与内容摘要；软件向量不能被批准为现场证据；缺信息给出拒绝/未知，不填补虚假默认值。
- 新工程分析 API 最大 JSON 输入 `8 MiB`，拒绝重复键、非有限数、布尔冒充数值、未知字段及越界样本；列表每页最多 `100`。
- 同身份库写入、审计、幂等回执在 `BEGIN IMMEDIATE` 事务中一起提交，并在该事务中复核当前会话/权限；计算在事务外，提交前复核。
- 新分析只产生建议、研究结果和影子策略。审批者不能复核自己的成果；历史输入/结果不覆盖，撤销与选用以追加事件表达。
- 算法只有显式受支持的版本；不加载用户提供的 pickle、动态 Python 或可执行模型；机器学习制品为有界数值 JSON。
- 按已确认的分层验证执行：每项针对性测试、独立审查，关键集成点再组合回归。不得将历史混合源码回归说成新版本一次全绿。
- 所有未解决问题、软件/现场边界和部署回退步骤在交付记录中列明；测试与私有部署凭据不公开上传。

## Review Focus

- Revocation/logout or role changes during long calculations must prevent result commit; Task 2 transaction tests.
- Same request after timeout must not create a second record or silently use changed input; Task 2 HTTP and Task 7 browser tests.
- Finite extreme numbers, flat curves, aliasing and unidentifiable physical parameters must refuse rather than emit NaN or fabricated diagnosis; Tasks 3–5 numerical tests.
- A 24-hour schedule must honor initial residual dwell, midnight starts and concurrent fleet power; Task 4 explicit timeline tests.
- Production mode must not seed fake wells, start legacy workers or expose a controller; Task 8 fresh/restart and Task 9 deployed checks.

---

### Task 1: Close inversion review findings I4/M4/M5

**Files:** Modify `frontend/inversion/workbench.js`, `frontend/inversion/charts.js`, `frontend/inversion/parameters.js`, `frontend/inversion/forms.js`; create focused helpers as needed in `frontend/inversion/`; tests in `tests/test_inversion_browser.py`, `tests/fixtures/inversion_*_check.html` and a focused pure reason-contract test if needed.

**Interfaces:** Preserve all existing exports and stored schemas. Consume backend `DIAGNOSTIC_REASONS`; produce readable reason + next-step mapping for every current code. Intermediate charts retain stored numeric values independently from final load validity, never promote unsupported line segments or metrics.

- [ ] Write failing browser tests for `dataset_changed`, `operating_conditions_unknown`, `archive_sync_invalid` and every allowed reason through a generated list from the real backend contract; unknown code must be safely identified, not generic accepted diagnosis.
- [ ] Write failing chart tests where load is invalid at a deadpoint but finite `power_W`, `theta_rad`, `drive_Nm` remain hover/keyboard readable with invalid/uncertain status; no line crosses unsupported interval or gap, no change to reference/prediction metrics. Legacy absent values remain unavailable, not zero.
- [ ] Implement focused `diagnostics.js` helper with cause + next step + code, integrate without losing bounded exact-current-run checks. Define explicit per-trace display support: known valid runs may connect adjacent points; retained finite values from prediction-invalid points are separately accessible isolated diagnostic points, clearly not valid load/physical interval evidence. No codec or solver rewrite.
- [ ] Reformat/extract dense parameter/form lifecycle helpers without behavior change; preserve API body/key, request uncertainty, role denial and disposal contracts. Add targeted recovery/switch regression if extraction exposes an uncovered branch.
- [ ] Run affected pure/browser tests with existing venv; `git diff --check`; self-review and commit only task paths. Do not run DB suites or touch servers.

### Task 2: Immutable engineering-study persistence and authenticated API

**Files:** Create `backend/studies/{__init__,contracts,storage,service,api,permissions}.py`, `backend/studies/schema.sql`; tests `tests/test_studies_storage.py`, `tests/test_studies_api.py`. HTTP wiring belongs to Task 8.

**Interfaces:** `StudyError(code,message,status=422)`; `validate_envelope(body)->dict`; `StudyService(database,auth,engines)` with `create(token,body)`, `list(token,kind=None,limit=50,offset=0)`, `get(token,id)`, `review(token,id,body)`, `select(token,kind,body)`, `export(token,id)`; `migrate_studies(database)` explicit. Engines map kind to `callable(payload)->dict` and version. `StudyApi(handler).dispatch()->bool` uses `/api/production-studies` namespace.

- [ ] Test strict JSON/envelope, source preservation, create/restart/read, current auth, duplicate request same/different body, concurrent replay, audit failure rollback, self-review refusal, stale version conflict, revoke/reselect, export audited before returning bytes.
- [ ] Implement records with UUID IDs, canonical finite JSON and SHA-256, append-only events/reviews/selections, bounded list/read and payload size, foreign key author identities, explicit schema version. Creation envelope: `{request_id,kind,title,source:{kind,description,references},payload}`; source kind `software_fixture|submitted_observation`, references max 32 bounded strings. Unknown fields refused. Review: `{request_id,expected_version,decision,comment}`; selection same plus `record_id`. Return committed record identity/digests/version, never raw auth data.
- [ ] Compute only after permission and validation; serialize commit with in-transaction current authorization and idempotency check. Engines cannot control SQL/paths, calculate outside transaction. Read/export current restrictions and immutable original hashes. Add per-kind supported statuses and fail-safe unavailable engine behavior, not fake success.
- [ ] Implement HTTP strict content type/length/duplicate keys/nonfinite/Origin, methods, 400/401/403/404/409/413/422 errors, limited data projections. Reuse existing session cookie and helpers without changing legacy API. Permission codes `studies.read`, `studies.create`, `studies.review`, `studies.select`, `studies.export`.
- [ ] Run targeted real temporary SQLite/loopback HTTP tests, self-review, commit; no global DB operations.

### Task 3: Reproducible condition baselines and BP training

**Files:** Create `backend/studies/{condition_data,condition_models,condition_evaluation}.py`, `tests/test_condition_studies.py`, `docs/工况模型软件验证.md`.

**Interfaces:** `train_conditions(payload)->dict`; `predict_conditions(payload)->dict` in `condition_models.py`. Plain JSON only. Detailed bounded data/model contract is fixed in the task supplement before dispatch; root records that supplement alongside this plan.

- [ ] Pin train/validation/test grouping, timestamp ordering, sample identity, feature units, train-only scaling, class coverage, unknown thresholds and finite operation budget with independent fixtures; write RED tests.
- [ ] Implement actual deterministic tanh/softmax backpropagation with recorded seed/loss history and bounded JSON weights, plus submitted threshold rules and nearest-centroid baseline on identical held-out rows; confusion matrices/macro F1/coverage and rejected samples, no training-set accuracy as test accuracy.
- [ ] Implement artifact validation and prediction with explicit confidence/OOD unknown; test malformed/overflow/nonconvergence/source provenance and unchanged reference inputs.
- [ ] Test three source-separated classes, reproducibility, leakage refusals, deliberate label alteration/feature drift, and nonzero gradient learning. Document algorithms and primary sources; commit after targeted tests.

### Task 4: Recovery fitting and finite-horizon shadow scheduling

**Files:** Create `backend/studies/{recovery,energy_schedule,energy_effects}.py`, `tests/test_recovery_studies.py`, `tests/test_energy_schedule.py`, `docs/恢复与影子优化方法.md`.

**Interfaces:** `fit_recovery(payload)->dict`, `optimize_schedule(payload)->dict`, `evaluate_effects(payload)->dict`. Supplement before dispatch pins observations/units/constraints; never call the legacy demo fitter with incompatible measurements.

- [ ] RED tests for known bounded recovery, flat/reversed/missing/sparse data, uncertainty/no extrapolation, and ill-conditioned fit.
- [ ] Implement explicit empirical exponential fit with measured residuals/profile uncertainty and applicability evidence; preserve original points and rejected-quality reasons.
- [ ] RED and implement finite daily schedule simulation and constrained candidate ranking, initial dwell/start count/midnight/storage balance/power and minimum production; explicit no-feasible status, no invented fallback.
- [ ] Implement measured baseline/treatment comparison with time/coverage/operating-condition checks and honest descriptive-vs-causal labeling. Test energy integral and unobserved intervals.
- [ ] Targeted tests, documented primary sources, self-review, commit.

### Task 5: Spectrum evidence and configurable protection replay

**Files:** Create `backend/studies/{spectrum,protection}.py`, `tests/test_motor_spectrum.py`, `tests/test_protection_replay.py`, `docs/电机诊断与保护软件边界.md`.

**Interfaces:** `analyze_spectrum(payload)->dict`, `replay_protection(payload)->dict`; detailed contract in supplement before dispatch. No hardware write.

- [ ] RED tests for exact frequency sinusoid/amplitude, DC/Hann compensation, missing quality/anti-alias, undersampled/unresolvable sideband, invalid slip, variable speed, clipped data and extreme finite input.
- [ ] Implement bounded real FFT, resolution and supplied uncertainty checks, main/sideband evidence; result never asserts confirmed broken bars or bar count; return inconclusive with explicit reasons when prerequisites fail.
- [ ] RED and implement duration/hysteresis/latch/manual-reset state machine from explicit settings, per-frame quality/time and stale/fault state, reset denied until safe interval; firmware/remote enable stays false.
- [ ] Test delayed trips, intermittent threshold crossing, reversed timestamps, reset while fault persists, missing phases, network-gap behavior, no default safe thresholds. Document sources, targeted tests, commit.

### Task 6: Open edge reference and integration examples

**Files:** Extend `assets/open-interface/HARDWARE.md`; create `assets/open-interface/EDGE-CONTROL-REFERENCE.md`, `backend/gateway/protection_reference.py`, tests `tests/test_edge_protection_reference.py`; update public kit builder/manifest with explicit whitelist.

**Interfaces:** Reuse Task 5 protection state machine in a bounded reference adapter; MCU/DSP mailbox/state events serialize validated fault/provenance, not control commands. Existing OWP uplink stays compatible.

- [ ] RED tests for boot-safe/inhibit, freshness, sequence replay, watchdog/reset and hardware-trip priority in reference adapter; no write method exposed.
- [ ] Implement deterministic reference adapter and conformance vectors; clarify BSP/ADC/isolated drivers/hardware interlock responsibilities, terminal capability handshake, local-only reset requirements.
- [ ] Update module-level reference diagrams/docs and open kit, verify whitelist bytes and no local data/credentials. State not certified schematics/PCB/firmware hardware proof. Commit.

### Task 7: Unified engineering pages and usable workflows

**Files:** Create `frontend/studies/{client,forms,charts,workbench}.js`, `frontend/pages/production-studies.js`, tests/browser fixtures; update `app.js`, `styles.css` only for study navigation/visuals.

**Interfaces:** Task 2 API, Tasks 3–5 payloads/results. Pages `engineering-overview`, `condition-lab`, `recovery-lab`, `motor-lab`, `study-review`. Use declared capabilities and permissions; navigate/import/edit/run/review/export remain real API actions.

- [ ] RED browser tests for authenticated list/create/result, per-kind input forms+JSON file import, charts/two-decimal hover+keyboard, submitted source display, rejected/no-feasible/unknown results.
- [ ] Implement input templates from explicit software fixtures via deliberate user button; no silently auto-created observations. Render meaningful numerical charts and comparison tables, source/version/digests/history.
- [ ] Test same-key timeout replay, stale selection/disposal/logout, self-review disabled and server refusal, role-only navigation, persistent refresh/export. Run real browser fixtures; commit.

### Task 8: Explicit production-review startup and owned deployment tooling

**Files:** Create `backend/review_runtime.py`, `tools/review_deploy.py`, `tests/test_review_runtime.py`, `tests/test_review_deploy.py`; modify `backend/http_server.py`, `server.py`, permission registration; document `docs/本机审查部署.md`.

**Interfaces:** `create_server(...,runtime_mode='demo')` backwards-compatible; `runtime_mode='review'` requires preinitialized identity/studies schema, disables seed/legacy simulator/workflow/control writes; Task 2 engine registry and API ready. Explicit review init creates new path only and references dedicated new PG settings.

- [ ] RED fresh/restart tests proving no seed, no default password, no legacy workers/actions, real login/permission health, pending schema refusal and current original/demo unchanged.
- [ ] Implement explicit identity initialization with random secrets delivered only through private output, not log/repo; grant roles once with admin full visibility but no self-review exception. Read startup checks, not grants/DDL.
- [ ] Implement new-owned PG provisioning using installed binaries and existing migrations only after validated empty target, new accounts, SCRAM loopback, explicit object ownership and minimal runtime grants. Do not reuse old test provision scripts, modify services or use broad deletion.
- [ ] Implement owned launch/stop/status/backup/restore-to-new-target with identity manifest, version and hash gates. Review HTTP loopback only, port collision refused; unknown PIDs never killed. Test helpers with temporary dirs/processes; actual deployment belongs to Task 9.

### Task 9: Integrated verification, review and local deployment

**Files:** Create `tools/verify_review_release.py`, `tests/test_review_release.py`, `docs/软件审查交付记录.md`; update annual-task response with exact software/field boundary.

- [ ] Run real HTTP/source-bound end-to-end studies for training/prediction/recovery/schedule/spectrum/protection/review/export, reject/rollback/role/version cases and restart persistence. Original 8765/frozen demo/test cluster remain untouched.
- [ ] Run fresh layered integration tests plus relevant unchanged regressions; record exact source/test counts, no skipped unsafe tests hidden as pass. Resolve independently reviewed load-bearing findings before deployment.
- [ ] Explicitly initialize new review runtime/databases, start owned loopback services, verify health and all module capabilities, log in via actual browser and check workflows. Backup and restore to a new isolated target; never clear existing records.
- [ ] Deliver URL, secure account retrieval, version, test evidence, stop/rollback instructions and field/hardware limitations. No automatic public push/merge in this deployment request.
