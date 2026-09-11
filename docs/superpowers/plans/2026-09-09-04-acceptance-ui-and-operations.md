# Acceptance UI and Operations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (\`- [ ]\`) syntax for tracking.

**Goal:** Replace the template-only prototype with a usable nine-module browser application, complete the acceptance flow, and package reliable one-click offline operation.

**Architecture:** A small ES-module frontend uses a shared API client, store, router, reusable tables/forms/dialogs, and focused page modules. It renders server-calculated data and performs real authenticated mutations; no page creates independent random business values.

**Tech Stack:** HTML5, CSS3, browser ES modules, SVG, Python unittest, Node.js syntax checks, Chrome and Edge manual browser verification.

**Spec:** docs/superpowers/specs/2026-09-09-oil-well-acceptance-system-design.md

## Global Constraints

- Consume all APIs and state conventions from plans 01 through 03.
- Keep the existing logo and professional blue industrial style.
- Remove the words 演示系统, 演示账号, 演示数据, and invalid dummy telephone numbers from visible copy.
- Provide visible loading, empty, success, validation, permission, conflict, and server-error states.
- Keep core workflows usable at a 1366 by 768 viewport and richer dashboards at 1920 by 1080.
- Use semantic buttons, labels, tables, headings, and keyboard-focus styles.
- Never invent client-side totals; dashboard and reports come from the API.

---

### Task 1: Frontend Shell, API Client, Session, and Router

**Files:**
- Modify: index.html
- Replace: app.js
- Create: frontend/api.js
- Create: frontend/store.js
- Create: frontend/router.js
- Create: frontend/components.js
- Create: frontend/format.js
- Create: frontend/pages/login.js
- Create: tests/test_static_assets.py
- Modify: styles.css

**Interfaces:**
- Produces: api.request(path: string, options?: object) -> Promise<any>
- Produces: store.getState(), store.setState(patch), store.subscribe(listener)
- Produces: router.register(pageId, loader), router.open(pageId, params), router.restore()
- Produces: components table, pagination, modal, toast, formField, statusTag, skeleton, errorPanel

- [x] **Step 1: Write failing asset and route tests**

    def test_index_uses_module_entry_and_has_accessible_regions(self):
        html = Path("index.html").read_text(encoding="utf-8")
        self.assertIn('type="module" src="./app.js"', html)
        self.assertIn('aria-label="一级功能导航"', html)
        self.assertIn('id="workspace"', html)

    def test_static_server_rejects_path_traversal(self):
        response = self.client.get("/../backend/database.py")
        self.assertEqual(response.status, 404)

- [x] **Step 2: Run tests and verify failure**

    python -m unittest tests.test_static_assets -v

Expected: module entry or static security assertion fails.

- [x] **Step 3: Implement shell and shared frontend modules**

Create login/session restoration, nine top modules, nested side navigation, closable tabs, route state, breadcrumb, global error boundary, 401 redirect, 403 panel, 409 conflict refresh prompt, toast queue, and modal focus return. API requests use same-origin cookies and abort after fifteen seconds.

- [x] **Step 4: Run tests and JavaScript syntax checks**

    python -m unittest tests.test_static_assets -v
    node --check app.js
    Get-ChildItem frontend -Filter *.js -Recurse | ForEach-Object { node --check $_.FullName }

Expected: all commands exit zero.

- [x] **Step 5: Commit**

    git add index.html app.js styles.css frontend tests/test_static_assets.py
    git commit -m "feat: build authenticated platform shell"

### Task 2: Dashboard, Well Detail, and Device Operations Pages

**Files:**
- Create: frontend/charts.js
- Create: frontend/pages/dashboard.js
- Create: frontend/pages/wells.js
- Create: frontend/pages/well-detail.js
- Create: frontend/pages/devices.js
- Create: frontend/pages/device-detail.js
- Create: frontend/pages/device-command.js
- Create: tests/test_page_contracts.py
- Modify: styles.css

**Interfaces:**
- Consumes: GET /api/dashboard, well APIs, device APIs, command APIs
- Produces: renderDashboard, renderWellList, renderWellDetail, renderDeviceList, renderDeviceDetail, renderDeviceCommand
- Produces: charts.line, charts.bar, charts.gauge, charts.dynagraph

- [x] **Step 1: Write failing page contract tests**

    def test_dashboard_payload_contains_drilldown_keys(self):
        data = self.client.get("/api/dashboard").json()["data"]
        self.assertTrue({"production", "devices", "quality", "alarms",
                         "strategies", "energy", "trends"}.issubset(data))

    def test_device_detail_contains_live_and_control_sections(self):
        source = Path("frontend/pages/device-detail.js").read_text(encoding="utf-8")
        for key in ["心跳", "采集通道", "实时寄存器", "通信报文", "自检记录"]:
            self.assertIn(key, source)

- [x] **Step 2: Run tests and verify failure**

    python -m unittest tests.test_page_contracts -v

Expected: missing page modules.

- [x] **Step 3: Implement operational pages**

Dashboard cards link to filtered details. Well list supports unit, group, state, low-output, keyword, pagination, and CSV export. Well detail uses summary, telemetry, power trend, production history, linked device, latest dynagraph, diagnosis, active strategy, alarm, and work-order tabs. Device pages show heartbeat age, firmware, channels, registers, message log, command history, and allowed scenario triggers.

- [x] **Step 4: Verify contracts and syntax**

    python -m unittest tests.test_page_contracts -v
    Get-ChildItem frontend -Filter *.js -Recurse | ForEach-Object { node --check $_.FullName }

Expected: tests and syntax checks pass.

- [x] **Step 5: Commit**

    git add frontend/charts.js frontend/pages styles.css tests/test_page_contracts.py
    git commit -m "feat: add live well and terminal workspaces"

### Task 3: Acquisition and Dynagraph Intelligence Pages

**Files:**
- Create: frontend/pages/acquisition.js
- Create: frontend/pages/data-quality.js
- Create: frontend/pages/dynagraphs.js
- Create: frontend/pages/dynagraph-analysis.js
- Create: frontend/pages/diagnosis-review.js
- Create: tests/test_analysis_pages.py
- Modify: styles.css

**Interfaces:**
- Consumes: acquisition, data-quality, dynagraph, analysis, diagnosis-review, and model-run APIs
- Produces: renderAcquisitionStream, renderQualityBoard, renderDynagraphList, renderDynagraphAnalysis, renderDiagnosisReview

- [x] **Step 1: Write failing content and action tests**

    def test_analysis_page_exposes_intermediate_sections(self):
        source = Path("frontend/pages/dynagraph-analysis.js").read_text(encoding="utf-8")
        for key in ["原始电功图", "滤波结果", "转矩换算", "示功图", "特征参数",
                    "诊断依据", "产液量计算"]:
            self.assertIn(key, source)

    def test_repair_action_changes_quality_status(self):
        event_id = self.create_quality_gap()
        response = self.client.post(f"/api/data-quality/{event_id}/repair", {})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.json()["data"]["status"], "repaired")

- [x] **Step 2: Run tests and verify failure**

    python -m unittest tests.test_analysis_pages -v

Expected: missing page modules.

- [x] **Step 3: Implement explainable acquisition and analysis UI**

Build a paced live stream with pause/resume, filters, sequence and checksum columns. Quality board drills from completeness cards to events and provides a repair action with progress. Analysis page overlays raw and smoothed electric curves, converted dynagraph, template comparison, feature table, rule hits, candidate conditions, formula factors, model version, and audit links. Review modal requires condition and comment.

- [x] **Step 4: Run tests and syntax checks**

    python -m unittest tests.test_analysis_pages -v
    Get-ChildItem frontend -Filter *.js -Recurse | ForEach-Object { node --check $_.FullName }

Expected: all commands exit zero.

- [ ] **Step 5: Commit**

    git add frontend/pages/acquisition.js frontend/pages/data-quality.js frontend/pages/dynagraphs.js frontend/pages/dynagraph-analysis.js frontend/pages/diagnosis-review.js styles.css tests/test_analysis_pages.py
    git commit -m "feat: visualize traceable dynagraph intelligence"

### Task 4: Energy Strategy, Approval, Command, Alarm, and Work-Order Pages

**Files:**
- Create: frontend/pages/energy-overview.js
- Create: frontend/pages/recovery-analysis.js
- Create: frontend/pages/strategy-editor.js
- Create: frontend/pages/strategy-center.js
- Create: frontend/pages/approvals.js
- Create: frontend/pages/commands.js
- Create: frontend/pages/alarms.js
- Create: frontend/pages/work-orders.js
- Create: tests/test_acceptance_flow.py
- Modify: styles.css

**Interfaces:**
- Consumes: energy, strategies, approvals, commands, alarms, and work-order APIs
- Produces: the full abnormal-well to measured-effect interaction flow

- [x] **Step 1: Write failing acceptance-flow test**

    def test_acceptance_flow_reaches_effective_strategy(self):
        login_as("engineer")
        diagnosis = run_supply_shortage("W-033")
        alarm = latest_alarm(diagnosis.id)
        draft = optimize_and_create_strategy("W-033")
        submit_strategy(draft.id, draft.version)
        login_as("admin")
        approve_strategy(draft.id, "参数与产量约束符合要求")
        command = dispatch_strategy(draft.id)
        process_commands()
        self.assertEqual(get_strategy(draft.id)["status"], "effective")
        self.assertEqual(get_command(command.id)["state"], "success")
        self.assertEqual(trace_alarm(alarm.id)["strategy_id"], draft.id)

- [x] **Step 2: Run test and verify failure**

    python -m unittest tests.test_acceptance_flow -v

Expected: UI or flow contract is incomplete.

- [x] **Step 3: Implement the end-to-end workflow UI**

Recovery page shows observations, fitted curve, tau, fit quality, constraints, ranked candidate table, and rejected-candidate reasons. Strategy editor validates and saves drafts. Center shows status history and immutable versions. Approval requires comment. Dispatch modal shows device, payload, expiry, safety check, and expected result. Command detail streams receipt stages. Alarm and order pages expose legal actions only and show a unified trace timeline.

- [x] **Step 4: Run workflow tests and syntax checks**

    python -m unittest tests.test_acceptance_flow tests.test_strategies tests.test_commands tests.test_workflows -v
    Get-ChildItem frontend -Filter *.js -Recurse | ForEach-Object { node --check $_.FullName }

Expected: all tests pass.

- [x] **Step 5: Commit**

    git add frontend/pages styles.css tests/test_acceptance_flow.py
    git commit -m "feat: complete adaptive control acceptance flow"

### Task 5: Reports, Administration, and Acceptance Evidence Center

**Files:**
- Create: frontend/pages/reports.js
- Create: frontend/pages/users.js
- Create: frontend/pages/roles.js
- Create: frontend/pages/audit.js
- Create: frontend/pages/system-health.js
- Create: frontend/pages/acceptance-center.js
- Create: tests/test_acceptance_center.py
- Modify: styles.css

**Interfaces:**
- Consumes: report, user, role, audit, health, model-run, and trace APIs
- Produces: printable reports, CSV downloads, role administration, read-only audit timeline, and requirement-to-evidence mapping

- [x] **Step 1: Write failing evidence tests**

    def test_acceptance_center_maps_all_task_requirements(self):
        data = self.client.get("/api/acceptance/evidence").json()["data"]
        codes = {item["code"] for item in data["requirements"]}
        self.assertEqual(codes, {"Y1-DYNAGRAPH", "Y1-ACQUISITION", "Y1-TRANSMISSION",
                                 "Y1-DECISION", "Y2-ENERGY", "Y2-DSP-MCU"})
        self.assertTrue(all(item["evidence"] for item in data["requirements"]))

    def test_audit_page_has_no_delete_action(self):
        source = Path("frontend/pages/audit.js").read_text(encoding="utf-8")
        self.assertNotIn("deleteAudit", source)

- [x] **Step 2: Run tests and verify failure**

    python -m unittest tests.test_acceptance_center -v

Expected: missing evidence API and page modules.

- [x] **Step 3: Implement management and evidence views**

Add server endpoint /api/acceptance/evidence that calculates evidence counts and latest linked records. Report pages use backend filters and download endpoints. Administration pages enforce role capabilities and conflict versions. Audit page supports filters and detail only. System health shows service, database, simulator, queue, disk, and latest backup status. Acceptance center links each annual requirement to live pages, tests, records, and trace identifiers without inventing intellectual-property filings.

- [x] **Step 4: Run evidence and report tests**

    python -m unittest tests.test_acceptance_center tests.test_reports tests.test_auth -v
    Get-ChildItem frontend -Filter *.js -Recurse | ForEach-Object { node --check $_.FullName }

Expected: all commands exit zero.

- [x] **Step 5: Commit**

    git add frontend/pages backend/api.py backend/http_server.py styles.css tests/test_acceptance_center.py
    git commit -m "feat: add reports and acceptance evidence center"

### Task 6: Full Verification, Backup, and Operator Handoff

**Files:**
- Create: backend/backup.py
- Create: tests/test_restart_and_backup.py
- Create: scripts/acceptance_smoke.py
- Create: docs/验收操作手册.md
- Create: docs/系统启动与恢复.md
- Modify: server.py
- Modify: start-platform.cmd
- Modify: stop-platform.cmd

**Interfaces:**
- Produces: backup_database(db: Database, destination: Path) -> BackupResult
- Produces: restore_database(source: Path, destination: Path) -> None
- Produces: python scripts/acceptance_smoke.py -> exit zero and a timestamped JSON report

- [ ] **Step 1: Write failing restart and backup tests**

    def test_restart_preserves_strategy_and_audit(self):
        strategy_id = create_effective_strategy()
        before = audit_count()
        restart_test_server()
        self.assertEqual(get_strategy(strategy_id)["status"], "effective")
        self.assertGreaterEqual(audit_count(), before)

    def test_backup_is_consistent_and_restorable(self):
        result = backup_database(self.db, self.backup_path)
        restored = Database(self.restore_path)
        restore_database(result.path, self.restore_path)
        self.assertEqual(restored.scalar("PRAGMA integrity_check"), "ok")
        self.assertEqual(restored.scalar("SELECT count(*) FROM wells"), 48)

- [ ] **Step 2: Run tests and verify failure**

    python -m unittest tests.test_restart_and_backup -v

Expected: missing backup module or restart helper.

- [ ] **Step 3: Implement backup, smoke script, and operator documentation**

Use sqlite3 Connection.backup into backups/platform-YYYYMMDD-HHMMSS.db, retain the latest ten, and verify integrity before reporting success. The smoke script starts a temporary server, checks health, login, dashboard, well detail, analysis, strategy optimization, approval, dispatch, command success, report CSV, audit trace, restart persistence, and shutdown. The Chinese operator guide lists accounts, the 15 to 20 minute main flow, four backup failure demonstrations, reset procedure, and recovery commands.

- [ ] **Step 4: Run final automated verification**

    python -m unittest discover -s tests -v
    python scripts/acceptance_smoke.py
    node --check app.js
    Get-ChildItem frontend -Filter *.js -Recurse | ForEach-Object { node --check $_.FullName }

Expected: all tests pass, smoke exits zero, and its JSON report records every stage as passed.

- [ ] **Step 5: Perform browser and offline verification**

Start through start-platform.cmd. In Chrome and Edge at 1366 by 768, complete login, well drilldown, scenario trigger, analysis, optimization, approval, dispatch, command receipt, work-order closure, report export, and audit trace. Disconnect external network access and repeat health, query, mutation, and CSV export. Restart the service and confirm the effective strategy remains.

- [ ] **Step 6: Commit**

    git add backend/backup.py server.py start-platform.cmd stop-platform.cmd scripts/acceptance_smoke.py tests/test_restart_and_backup.py docs/验收操作手册.md docs/系统启动与恢复.md
    git commit -m "test: verify offline acceptance package"
