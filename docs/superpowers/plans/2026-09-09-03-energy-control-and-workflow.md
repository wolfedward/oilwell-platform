# Energy Control and Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (\`- [ ]\`) syntax for tracking.

**Goal:** Build low-output well recovery analysis, adaptive start/stop optimization, approval and command state machines, alarm/work-order closure, and effect reporting.

**Architecture:** Pure energy algorithms generate ranked strategy candidates from persisted production and recovery observations. Transactional services enforce state transitions and turn approved strategies into virtual terminal commands with receipts, retries, rollback, and audit links.

**Tech Stack:** Python 3.13 standard library, SQLite, unittest, CSV generation.

**Spec:** docs/superpowers/specs/2026-09-09-oil-well-acceptance-system-design.md

## Global Constraints

- Consume database, authentication, simulator, acquisition, and analysis interfaces from plans 01 and 02.
- Keep every strategy version immutable after submission.
- Reject unsafe control values on the server even if the browser bypasses validation.
- Link alarm, diagnosis, strategy, approval, command, work order, and audit identifiers.
- Express predicted and measured energy values in kWh and liquid production in cubic metres per day.
- Use configurable retry count three and command expiry fifteen minutes.

---

### Task 1: Recovery Curve Fitting and Adaptive Cycle Optimizer

**Files:**
- Create: backend/algorithms/energy.py
- Create: tests/fixtures/recovery_cases.json
- Create: tests/test_energy.py

**Interfaces:**
- Produces: fit_recovery_curve(observations: list[RecoveryPoint]) -> RecoveryModel
- Produces: optimize_cycle(model: RecoveryModel, constraints: StrategyConstraints, motor_kw: float) -> list[CycleCandidate]
- RecoveryModel fields: capacity, tau_minutes, baseline, r_squared
- CycleCandidate fields: on_minutes, off_minutes, cycles_per_day, predicted_liquid_m3, predicted_energy_kwh, saving_rate, feasible, violations

- [x] **Step 1: Write failing recovery and optimizer tests**

    def test_fit_recovers_time_constant(self):
        model = fit_recovery_curve(load_fixture("low_output_a"))
        self.assertAlmostEqual(model.tau_minutes, 86.0, delta=8.0)
        self.assertGreater(model.r_squared, 0.95)

    def test_optimizer_keeps_production_constraint_and_sorts_energy(self):
        candidates = optimize_cycle(model(capacity=1.0, tau_minutes=86),
                                    constraints(min_on=20, max_off=180,
                                                min_liquid=2.4, max_starts=12),
                                    motor_kw=18.5)
        feasible = [item for item in candidates if item.feasible]
        self.assertGreater(len(feasible), 2)
        self.assertTrue(all(item.predicted_liquid_m3 >= 2.4 for item in feasible))
        self.assertEqual(feasible, sorted(feasible, key=lambda item: item.predicted_energy_kwh))

- [x] **Step 2: Run tests and verify failure**

    python -m unittest tests.test_energy -v

Expected: missing energy algorithm module.

- [x] **Step 3: Implement curve fitting and search**

Fit q(t)=baseline+capacity*(1-exp(-t/tau)) by deterministic grid search over tau 10 through 720 minutes and least-squares capacity. Enumerate on-times in ten-minute steps and off-times in fifteen-minute steps, calculate cycles per day, recovered fillage, predicted liquid, runtime, energy, starts, and savings against continuous operation. Return feasible and rejected candidates with explicit violation text.

- [x] **Step 4: Run optimizer tests**

    python -m unittest tests.test_energy -v

Expected: fixture regressions, infeasible constraints, zero capacity, and deterministic sorting pass.

- [x] **Step 5: Commit**

    git add backend/algorithms/energy.py tests/fixtures/recovery_cases.json tests/test_energy.py
    git commit -m "feat: optimize adaptive pumping cycles"

### Task 2: Strategy Versions and Approval State Machine

**Files:**
- Create: backend/services/strategies.py
- Create: backend/state_machine.py
- Create: tests/test_strategies.py
- Modify: backend/api.py
- Modify: backend/http_server.py

**Interfaces:**
- Produces: transition(current: str, target: str, allowed: dict[str, set[str]]) -> None
- Produces: StrategyService.optimize(well_id: str, constraints: dict, actor_id: int) -> StrategyDraft
- Produces: StrategyService.submit(strategy_id: int, version: int, actor_id: int) -> Strategy
- Produces: StrategyService.decide(strategy_id: int, decision: str, comment: str, actor_id: int) -> Strategy
- Produces: GET/POST /api/strategies and POST actions optimize, submit, approve, reject

- [x] **Step 1: Write failing state tests**

    def test_submitted_strategy_is_immutable(self):
        strategy = service.create_draft("W-033", candidate(), actor_id=2)
        service.submit(strategy.id, strategy.version, actor_id=2)
        with self.assertRaises(InvalidState):
            service.update(strategy.id, {"off_minutes": 240}, strategy.version, actor_id=2)

    def test_reviewer_cannot_approve_own_submission(self):
        strategy = submitted_strategy(created_by=2)
        with self.assertRaises(ValidationError):
            service.decide(strategy.id, "approve", "同意", actor_id=2)

- [x] **Step 2: Run tests and verify failure**

    python -m unittest tests.test_strategies -v

Expected: missing strategy service.

- [x] **Step 3: Implement strategy lifecycle**

Allow draft to pending_approval, pending_approval to approved or rejected, approved to pending_dispatch, and effective to paused or rolled_back. Copy an approved strategy before any modification. Require approval comment, distinct approver, well safety limits, and optimistic version. Write approval and audit rows in the same transaction.

- [x] **Step 4: Run strategy tests**

    python -m unittest tests.test_strategies -v

Expected: legal transitions pass and illegal, stale, unsafe, and self-approval cases fail with stable error codes.

- [x] **Step 5: Commit**

    git add backend/services/strategies.py backend/state_machine.py backend/api.py backend/http_server.py tests/test_strategies.py
    git commit -m "feat: add versioned energy strategy approval"

### Task 3: Device Command Dispatch, Receipts, Retry, and Rollback

**Files:**
- Create: backend/services/commands.py
- Create: tests/test_commands.py
- Modify: backend/simulator.py
- Modify: backend/services/strategies.py
- Modify: backend/api.py

**Interfaces:**
- Produces: CommandService.dispatch_strategy(strategy_id: int, actor_id: int) -> DeviceCommand
- Produces: CommandService.process(now: datetime) -> ProcessSummary
- Produces: CommandService.rollback(strategy_id: int, actor_id: int, reason: str) -> DeviceCommand
- Produces: POST /api/strategies/{id}/dispatch and POST /api/strategies/{id}/rollback
- Produces: GET /api/commands and GET /api/commands/{id}

- [x] **Step 1: Write failing command tests**

    def test_online_terminal_returns_all_receipt_stages(self):
        command = service.dispatch_strategy(approved_strategy_id, actor_id=3)
        service.process(at("2026-06-22T10:00:00+08:00"))
        states = db.column("SELECT state FROM command_attempts WHERE command_id=? ORDER BY id",
                           (command.id,))
        self.assertEqual(states, ["sent", "received", "validated", "executing", "success"])

    def test_offline_terminal_waits_then_retries(self):
        set_device_state("D-033", "offline")
        command = service.dispatch_strategy(approved_strategy_id, actor_id=3)
        service.process(at("2026-06-22T10:00:00+08:00"))
        self.assertEqual(service.get(command.id).state, "queued")
        set_device_state("D-033", "online")
        service.process(at("2026-06-22T10:01:00+08:00"))
        self.assertEqual(service.get(command.id).state, "success")

- [x] **Step 2: Run tests and verify failure**

    python -m unittest tests.test_commands -v

Expected: missing command service.

- [x] **Step 3: Implement command processing**

Create a signed command payload containing device, strategy version, on/off minutes, issue time, expiry, nonce, and SHA-256 signature. Enforce terminal online state, expiry, maximum three attempts, fifteen-minute validity, safe ranges, and idempotency key. Persist each receipt stage. On success mark strategy effective; on terminal rejection or exhausted retry mark failed and create an alarm. Rollback sends the previous effective version.

- [x] **Step 4: Run command and strategy tests**

    python -m unittest tests.test_commands tests.test_strategies -v

Expected: success, offline queue, timeout retry, unsafe rejection, idempotency, and rollback pass.

- [x] **Step 5: Commit**

    git add backend/services/commands.py backend/simulator.py backend/services/strategies.py backend/api.py tests/test_commands.py
    git commit -m "feat: execute traceable terminal commands"

### Task 4: Alarm and Work-Order Closure

**Files:**
- Create: backend/services/workflows.py
- Create: tests/test_workflows.py
- Modify: backend/services/analysis.py
- Modify: backend/services/commands.py
- Modify: backend/api.py

**Interfaces:**
- Produces: WorkflowService.create_alarm(source_type: str, source_id: str, severity: str, details: dict) -> Alarm
- Produces: WorkflowService.transition_alarm(id: int, target: str, actor_id: int, comment: str) -> Alarm
- Produces: WorkflowService.create_work_order(alarm_id: int, kind: str, assignee_id: int, actor_id: int) -> WorkOrder
- Produces: WorkflowService.transition_work_order(id: int, target: str, actor_id: int, comment: str) -> WorkOrder
- Produces: GET/POST /api/alarms and GET/POST /api/work-orders with transition actions

- [ ] **Step 1: Write failing linked-flow test**

    def test_diagnosis_to_closed_work_order_is_traceable(self):
        diagnosis = run_abnormal_analysis("W-033")
        alarm = db.query_one("SELECT * FROM alarms WHERE source_id=?", (str(diagnosis.id),))
        order = workflow.create_work_order(alarm["id"], "diagnosis", 4, actor_id=2)
        workflow.transition_work_order(order.id, "accepted", 4, "已接单")
        workflow.transition_work_order(order.id, "processing", 4, "现场复核")
        workflow.transition_work_order(order.id, "review", 4, "处理完成")
        closed = workflow.transition_work_order(order.id, "closed", 2, "复核通过")
        self.assertEqual(closed.status, "closed")
        self.assertEqual(workflow.trace(order.id)["diagnosis_id"], diagnosis.id)

- [ ] **Step 2: Run test and verify failure**

    python -m unittest tests.test_workflows -v

Expected: missing workflow service.

- [ ] **Step 3: Implement alarm and order state machines**

Implement alarm states new, confirmed, assigned, processing, closed, false_positive, and escalated. Implement work-order states pending, accepted, processing, review, closed, and overdue. Require comments for close, false-positive, reject, and rollback actions. Schedule overdue evaluation from due_at and record every transition in work_order_logs and audit_logs.

- [ ] **Step 4: Run linked subsystem tests**

    python -m unittest tests.test_workflows tests.test_analysis_api tests.test_commands -v

Expected: all linked-flow tests pass.

- [ ] **Step 5: Commit**

    git add backend/services/workflows.py backend/services/analysis.py backend/services/commands.py backend/api.py tests/test_workflows.py
    git commit -m "feat: close alarms through work orders"

### Task 5: Effect Evaluation and Server-Side Reports

**Files:**
- Create: backend/services/reports.py
- Create: tests/test_reports.py
- Modify: backend/api.py
- Modify: backend/http_server.py

**Interfaces:**
- Produces: ReportService.energy_effect(strategy_id: int, from_time: datetime, to_time: datetime) -> EnergyEffect
- Produces: ReportService.csv(report_type: str, filters: dict) -> bytes
- Produces: GET /api/reports/production, /api/reports/diagnosis, /api/reports/data-quality, /api/reports/strategy, /api/reports/energy, /api/reports/work-orders
- Produces: GET /api/reports/{type}.csv

- [ ] **Step 1: Write failing report reconciliation tests**

    def test_energy_report_reconciles_with_telemetry(self):
        report = service.energy_effect(strategy_id, start, end)
        measured = db.scalar("SELECT sum(power_kw * sample_hours) FROM telemetry WHERE well_id=? AND ts BETWEEN ? AND ?",
                             (report.well_id, start.isoformat(), end.isoformat()))
        self.assertAlmostEqual(report.after_energy_kwh, measured, places=3)
        self.assertAlmostEqual(report.saving_rate,
                               1 - report.after_energy_kwh / report.baseline_energy_kwh,
                               places=6)

    def test_csv_contains_filter_and_utf8_bom(self):
        content = service.csv("energy", {"unit_id": "U-01"})
        self.assertTrue(content.startswith(b"\\xef\\xbb\\xbf"))
        self.assertIn("U-01", content.decode("utf-8-sig"))

- [ ] **Step 2: Run tests and verify failure**

    python -m unittest tests.test_reports -v

Expected: missing report service.

- [ ] **Step 3: Implement computed reports and CSV export**

Compute all totals from source tables, include filter metadata and generated_at, and use csv.writer with UTF-8 BOM. Energy evaluation compares a configurable pre-strategy baseline window with the post-effective window and returns energy, runtime, liquid, start count, and savings. Do not store independent totals that can diverge from detail rows.

- [ ] **Step 4: Run the full backend suite**

    python -m unittest discover -s tests -v

Expected: all backend tests pass.

- [ ] **Step 5: Commit**

    git add backend/services/reports.py backend/api.py backend/http_server.py tests/test_reports.py
    git commit -m "feat: report measured energy effects"
