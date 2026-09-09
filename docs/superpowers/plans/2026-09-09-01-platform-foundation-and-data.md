# Platform Foundation and Data Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (\`- [ ]\`) syntax for tracking.

**Goal:** Build the offline Python/SQLite runtime, deterministic project dataset, authentication, audit trail, and core well APIs used by later subsystems.

**Architecture:** A Python standard-library HTTP service owns business state and serves JSON APIs plus static browser assets. SQLite is migrated at startup and seeded once with internally consistent wells, devices, telemetry, production history, and workflow records.

**Tech Stack:** Python 3.13 standard library, SQLite, HTML/CSS/JavaScript, unittest.

**Spec:** docs/superpowers/specs/2026-09-09-oil-well-acceptance-system-design.md

## Global Constraints

- Run offline on the acceptance computer with Python 3.13, Chrome, or Edge.
- Require no pip, npm, cloud account, or external database.
- Store persistent state under data/ and logs under logs/.
- Generate fixed-seed test data and retain source_data_source inside database records.
- Return JSON as an object with ok plus data, or ok plus error.code and error.message.
- Write Asia/Shanghai timestamps with an explicit +08:00 offset.
- Validate authorization and append an audit record for every mutation.

---

### Task 1: Runtime Skeleton and Health Endpoint

**Files:**
- Create: .gitignore
- Create: server.py
- Create: backend/__init__.py
- Create: backend/config.py
- Create: backend/http_server.py
- Create: tests/__init__.py
- Create: tests/test_health.py
- Create: start-platform.cmd
- Create: stop-platform.cmd

**Interfaces:**
- Produces: backend.http_server.create_server(host: str, port: int) -> ThreadingHTTPServer
- Produces: GET /api/health -> service status, database status, simulator status, version, and current time

- [ ] **Step 1: Write the failing health test**

    class HealthTests(unittest.TestCase):
        def test_health_returns_version_and_timezone(self):
            server, thread = start_test_server()
            payload = get_json(server.server_port, "/api/health")
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["data"]["version"], "2.0.0")
            self.assertTrue(payload["data"]["time"].endswith("+08:00"))
            server.shutdown()
            thread.join()

- [ ] **Step 2: Run the test and verify failure**

    python -m unittest tests.test_health -v

Expected: import failure for backend.http_server.

- [ ] **Step 3: Implement the minimal server**

Implement Config with host 127.0.0.1, default port 8765, project root, database path, log path, and version 2.0.0. Implement BaseHTTPRequestHandler routing for /api/health and static files. Add command-line options --host, --port, --open, and --stop. start-platform.cmd launches pythonw server.py --open; stop-platform.cmd invokes python server.py --stop.

- [ ] **Step 4: Run focused and syntax tests**

    python -m unittest tests.test_health -v
    python -m py_compile server.py backend/config.py backend/http_server.py

Expected: all commands exit zero.

- [ ] **Step 5: Commit**

    git add .gitignore server.py backend tests/test_health.py start-platform.cmd stop-platform.cmd
    git commit -m "feat: add offline platform runtime"

### Task 2: SQLite Schema and Transaction Boundary

**Files:**
- Create: backend/database.py
- Create: backend/schema.sql
- Create: tests/test_database.py
- Modify: backend/http_server.py

**Interfaces:**
- Produces: Database(path: Path)
- Produces: Database.connection() -> context-managed sqlite3.Connection
- Produces: Database.migrate() -> None
- Produces: Database.transaction(actor_id: int | None, action: str, object_type: str, object_id: str | None)
- Produces: Database.query_one(), query_all(), scalar(), column(), execute(), and executemany()

- [ ] **Step 1: Write failing migration tests**

    def test_migrate_creates_core_tables(self):
        db = Database(self.path)
        db.migrate()
        names = db.table_names()
        self.assertTrue({"units", "well_groups", "wells", "devices",
                         "telemetry", "audit_logs"}.issubset(names))

    def test_foreign_keys_are_enforced(self):
        db = Database(self.path)
        db.migrate()
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("INSERT INTO wells(id, group_id, name) VALUES(?, ?, ?)",
                       ("W-X", "missing", "invalid"))

- [ ] **Step 2: Run the tests and verify failure**

    python -m unittest tests.test_database -v

Expected: import failure for backend.database.

- [ ] **Step 3: Implement schema and database wrapper**

Create normalized tables named in section 6.2 of the spec, enable foreign_keys and WAL, set busy_timeout to 5000 ms, and expose query_one, query_all, execute, executemany, and transaction helpers. The schema must include created_at, updated_at, status, actor_id, and source_type where applicable.

- [ ] **Step 4: Run database tests**

    python -m unittest tests.test_database -v

Expected: both tests pass and a second migrate call is idempotent.

- [ ] **Step 5: Commit**

    git add backend/database.py backend/schema.sql backend/http_server.py tests/test_database.py
    git commit -m "feat: add persistent platform schema"

### Task 3: Deterministic Project Dataset

**Files:**
- Create: backend/seed.py
- Create: tests/test_seed.py
- Modify: server.py

**Interfaces:**
- Produces: seed_database(db: Database, seed: int = 202606) -> SeedSummary
- Produces: SeedSummary(units: int, groups: int, wells: int, devices: int, telemetry: int)

- [ ] **Step 1: Write failing consistency tests**

    def test_seed_counts_and_relations(self):
        summary = seed_database(self.db)
        self.assertEqual(summary.units, 4)
        self.assertEqual(summary.groups, 8)
        self.assertEqual(summary.wells, 48)
        self.assertEqual(summary.devices, 48)
        self.assertGreaterEqual(summary.telemetry, 48 * 30 * 24)
        self.assertEqual(self.db.scalar("SELECT count(*) FROM wells WHERE is_low_output=1"), 16)

    def test_seed_is_idempotent_and_deterministic(self):
        first = seed_database(self.db)
        second = seed_database(self.db)
        self.assertEqual(first, second)
        row = self.db.query_one("SELECT daily_fluid FROM production_daily ORDER BY well_id, day LIMIT 1")
        self.assertEqual(round(row["daily_fluid"], 3), 2.846)

- [ ] **Step 2: Run tests and verify failure**

    python -m unittest tests.test_seed -v

Expected: import failure for backend.seed.

- [ ] **Step 3: Implement fixed-seed generation**

Create four units, eight groups, 48 wells, one device per well, 90 daily production rows per well, 30 hourly telemetry days per well, and seeded alarms, work orders, strategies, and device events. Derive values from each well profile so power, stroke, frequency, pump efficiency, production, and diagnosis remain correlated. Store source_type as simulator and never regenerate an existing seeded dataset.

- [ ] **Step 4: Run seed and integrity tests**

    python -m unittest tests.test_seed tests.test_database -v

Expected: all tests pass and PRAGMA foreign_key_check returns no rows.

- [ ] **Step 5: Commit**

    git add backend/seed.py server.py tests/test_seed.py
    git commit -m "feat: seed consistent oil well history"

### Task 4: Local Authentication, Roles, and Audit

**Files:**
- Create: backend/auth.py
- Create: backend/audit.py
- Create: tests/test_auth.py
- Modify: backend/http_server.py
- Modify: backend/seed.py

**Interfaces:**
- Produces: AuthService.login(username: str, password: str) -> Session
- Produces: AuthService.require(token: str, permission: str) -> User
- Produces: AuditService.record(actor_id: int, action: str, object_type: str, object_id: str, details: dict) -> int
- Produces: POST /api/auth/login, POST /api/auth/logout, GET /api/auth/me
- Produces: GET/POST/PATCH /api/users and GET/POST/PATCH /api/roles with permission checks and optimistic versions

- [ ] **Step 1: Write failing role and audit tests**

    def test_readonly_user_cannot_mutate(self):
        token = self.auth.login("reviewer", "Review@2026").token
        with self.assertRaises(PermissionDenied):
            self.auth.require(token, "strategy.approve")

    def test_mutation_audit_contains_before_and_after(self):
        audit_id = self.audit.record(1, "update", "well", "W-001",
                                     {"before": {"status": "running"},
                                      "after": {"status": "stopped"}})
        row = self.db.query_one("SELECT details_json FROM audit_logs WHERE id=?", (audit_id,))
        self.assertIn('"before"', row["details_json"])
        self.assertIn('"after"', row["details_json"])

- [ ] **Step 2: Run tests and verify failure**

    python -m unittest tests.test_auth -v

Expected: import failures for backend.auth and backend.audit.

- [ ] **Step 3: Implement authentication and permissions**

Use hashlib.pbkdf2_hmac with per-user salt, random 32-byte session tokens, eight-hour expiry, and HttpOnly SameSite=Strict cookies. Seed admin, engineer, algorithm, maintenance, and reviewer roles with explicit permissions. Implement permission-checked user and role list, create, enable/disable, password-reset, and permission-update routes with optimistic versions. Implement audit writes inside the same transaction as mutations.

- [ ] **Step 4: Run authentication tests**

    python -m unittest tests.test_auth -v

Expected: all tests pass, invalid passwords return 401, and forbidden actions return 403.

- [ ] **Step 5: Commit**

    git add backend/auth.py backend/audit.py backend/http_server.py backend/seed.py tests/test_auth.py
    git commit -m "feat: add local roles and audit trail"

### Task 5: Dashboard, Well, and Telemetry APIs

**Files:**
- Create: backend/api.py
- Create: backend/services/__init__.py
- Create: backend/services/wells.py
- Create: tests/test_well_api.py
- Modify: backend/http_server.py

**Interfaces:**
- Produces: GET /api/dashboard
- Produces: GET /api/wells?unit_id=&group_id=&status=&keyword=&page=&page_size=
- Produces: GET /api/wells/{well_id}
- Produces: GET /api/wells/{well_id}/telemetry?from=&to=&interval=
- Produces: PATCH /api/wells/{well_id}/status with status, reason, and version

- [ ] **Step 1: Write failing API tests**

    def test_well_detail_links_business_objects(self):
        data = self.client.get("/api/wells/W-001")["data"]
        self.assertEqual(data["well"]["id"], "W-001")
        self.assertIn("device", data)
        self.assertIn("latest_telemetry", data)
        self.assertIn("latest_diagnosis", data)
        self.assertIn("active_strategy", data)

    def test_status_update_rejects_stale_version(self):
        response = self.client.patch("/api/wells/W-001/status",
                                     {"status": "stopped", "reason": "检修", "version": -1})
        self.assertEqual(response.status, 409)

- [ ] **Step 2: Run tests and verify failure**

    python -m unittest tests.test_well_api -v

Expected: routes return 404 before implementation.

- [ ] **Step 3: Implement query services and routes**

Implement parameterized SQL, page_size range 1 through 100, ISO time validation, interval aggregation for raw, 15m, 1h, and 1d, and optimistic version checks for status changes. Dashboard totals must be computed from the same tables used by detail pages.

- [ ] **Step 4: Run the complete foundation suite**

    python -m unittest discover -s tests -v

Expected: all foundation tests pass.

- [ ] **Step 5: Commit**

    git add backend/api.py backend/services backend/http_server.py tests/test_well_api.py
    git commit -m "feat: expose dashboard and well data APIs"
