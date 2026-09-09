# Acquisition and Dynagraph Intelligence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (\`- [ ]\`) syntax for tracking.

**Goal:** Build the virtual DSP+MCU acquisition pipeline, data-quality controls, electric-card conversion, feature extraction, diagnosis, and liquid-production estimation.

**Architecture:** A deterministic simulator emits signed JSON-like device frames into a transactional acquisition service. Pure algorithm modules transform stored samples and persist explainable intermediate values and final diagnoses.

**Tech Stack:** Python 3.13 standard library, SQLite, unittest, browser SVG rendering supplied by plan 04.

**Spec:** docs/superpowers/specs/2026-09-09-oil-well-acceptance-system-design.md

## Global Constraints

- Consume Database, AuthService, AuditService, and HTTP response conventions from plan 01.
- Keep algorithm functions pure; persistence belongs to service classes.
- Retain every raw frame and store rejection or repair reasons.
- Make simulations deterministic for a given device, scenario, and timestamp.
- Persist formula inputs, intermediate values, rule hits, and model version with every analysis.
- Do not claim a connection to physical hardware.

---

### Task 1: Virtual DSP+MCU Terminal State Machine

**Files:**
- Create: backend/simulator.py
- Create: backend/protocol.py
- Create: tests/test_simulator.py
- Modify: backend/schema.sql
- Modify: server.py

**Interfaces:**
- Produces: encode_frame(device_id: str, sequence: int, payload: dict) -> bytes
- Produces: decode_frame(frame: bytes) -> DeviceFrame
- Produces: VirtualTerminal.tick(now: datetime) -> list[bytes]
- Produces: VirtualTerminal.apply_scenario(name: str, duration_minutes: int) -> ScenarioState
- Produces: POST /api/devices/{device_id}/scenarios
- Produces: GET /api/devices, GET /api/devices/{device_id}, GET /api/devices/{device_id}/registers
- Produces: PATCH /api/devices/{device_id}/channels/{channel_id} with safety validation and optimistic version

- [ ] **Step 1: Write failing protocol and state tests**

    def test_frame_round_trip_and_checksum(self):
        raw = encode_frame("D-001", 7, {"current": 18.4, "stroke": 3.2})
        frame = decode_frame(raw)
        self.assertEqual(frame.device_id, "D-001")
        self.assertEqual(frame.sequence, 7)
        self.assertEqual(frame.payload["current"], 18.4)

    def test_supply_shortage_changes_fillage_not_identity(self):
        terminal = VirtualTerminal(profile_for("D-001"), seed=11)
        normal = terminal.tick(at("2026-06-22T10:00:00+08:00"))
        terminal.apply_scenario("supply_shortage", 30)
        abnormal = terminal.tick(at("2026-06-22T10:01:00+08:00"))
        self.assertLess(extract_fillage(abnormal), extract_fillage(normal))
        self.assertEqual(decode_frame(abnormal[0]).device_id, "D-001")

- [ ] **Step 2: Run tests and verify failure**

    python -m unittest tests.test_simulator -v

Expected: missing backend.simulator and backend.protocol.

- [ ] **Step 3: Implement terminal protocol and scenarios**

Use a frame header, version, device id, sequence, timestamp, payload length, payload JSON, and SHA-256 checksum. Implement online, offline, booting, running, stopped, fault, and local-control states. Implement normal, supply_shortage, pump_leak, empty_pump, sensor_drift, packet_loss, and offline scenarios. Add paged device list/detail, live-register, and channel-update routes; channel changes enforce configured ranges and version checks. Background ticking starts only after database migration and stops cleanly with the HTTP server.

- [ ] **Step 4: Run simulator tests**

    python -m unittest tests.test_simulator -v

Expected: round-trip, checksum rejection, deterministic scenario, and state transition tests pass.

- [ ] **Step 5: Commit**

    git add backend/simulator.py backend/protocol.py backend/schema.sql server.py tests/test_simulator.py
    git commit -m "feat: simulate dsp mcu terminal telemetry"

### Task 2: Acquisition, Validation, Quality Events, and Replay

**Files:**
- Create: backend/services/acquisition.py
- Create: tests/test_acquisition.py
- Modify: backend/api.py
- Modify: backend/http_server.py

**Interfaces:**
- Produces: AcquisitionService.ingest(raw: bytes) -> IngestResult
- Produces: AcquisitionService.replay(device_id: str, start_sequence: int, end_sequence: int) -> ReplayResult
- Produces: GET /api/acquisition/stream, GET /api/acquisition/messages, GET /api/data-quality
- Produces: POST /api/data-quality/{event_id}/repair

- [ ] **Step 1: Write failing ingest and replay tests**

    def test_sequence_gap_creates_quality_event(self):
        service.ingest(frame(sequence=20))
        result = service.ingest(frame(sequence=22))
        self.assertEqual(result.accepted, True)
        event = db.query_one("SELECT * FROM data_quality_events WHERE kind='sequence_gap'")
        self.assertEqual(event["missing_from"], 21)
        self.assertEqual(event["missing_to"], 21)

    def test_replay_closes_gap_and_preserves_raw_frame(self):
        event_id = create_gap(20, 22)
        result = service.replay("D-001", 21, 21)
        self.assertEqual(result.restored, 1)
        self.assertEqual(db.scalar("SELECT status FROM data_quality_events WHERE id=?", (event_id,)), "repaired")
        self.assertEqual(db.scalar("SELECT count(*) FROM raw_messages WHERE sequence=21"), 1)

- [ ] **Step 2: Run tests and verify failure**

    python -m unittest tests.test_acquisition -v

Expected: missing acquisition service.

- [ ] **Step 3: Implement transactional ingestion**

Validate checksum, device identity, timestamp skew, sequence, numeric ranges, duplicate identity, and payload schema. Store raw frame first, then accepted telemetry or rejection. Create quality events for gaps, drift, outliers, duplicates, and delay. Replay must be idempotent and link restored messages to the quality event.

- [ ] **Step 4: Run acquisition tests**

    python -m unittest tests.test_acquisition -v

Expected: acceptance, rejection, gap, replay, duplicate, and outlier cases pass.

- [ ] **Step 5: Commit**

    git add backend/services/acquisition.py backend/api.py backend/http_server.py tests/test_acquisition.py
    git commit -m "feat: add reliable acquisition and replay"

### Task 3: Electric Card Conversion and Feature Extraction

**Files:**
- Create: backend/algorithms/__init__.py
- Create: backend/algorithms/dynagraph.py
- Create: tests/fixtures/dynagraph_cases.json
- Create: tests/test_dynagraph.py

**Interfaces:**
- Produces: smooth(samples: list[float], window: int = 5) -> list[float]
- Produces: split_cycle(samples: list[ElectricalSample], strokes_per_minute: float) -> list[ElectricalSample]
- Produces: convert_electric_card(samples: list[ElectricalSample], params: MechanismParams) -> DynagraphResult
- Produces: extract_features(points: list[LoadDisplacementPoint]) -> DynagraphFeatures

- [ ] **Step 1: Write failing algorithm tests**

    def test_conversion_produces_closed_physical_curve(self):
        samples, params = fixture("normal")
        result = convert_electric_card(samples, params)
        self.assertEqual(len(result.points), 120)
        self.assertAlmostEqual(result.points[0].displacement,
                               result.points[-1].displacement, delta=0.08)
        self.assertGreater(result.features.area, 10.0)
        self.assertGreater(result.features.max_load, result.features.min_load)

    def test_supply_shortage_has_lower_fillage_and_area(self):
        normal = analyze_fixture("normal")
        shortage = analyze_fixture("supply_shortage")
        self.assertLess(shortage.features.fillage, normal.features.fillage)
        self.assertLess(shortage.features.area, normal.features.area)

- [ ] **Step 2: Run tests and verify failure**

    python -m unittest tests.test_dynagraph -v

Expected: missing algorithm module.

- [ ] **Step 3: Implement the engineering pipeline**

Apply a centered five-point moving average, normalize one stroke cycle to 120 points, estimate motor torque as power divided by angular velocity, apply gear ratio and mechanical efficiency, subtract crank-angle balance torque, and calculate polished-rod load. Calculate displacement from normalized crank angle and stroke length. Return raw, smoothed, intermediate torque, final points, features, formula parameters, warnings, and model version dynagraph-2.0.

- [ ] **Step 4: Run algorithm tests**

    python -m unittest tests.test_dynagraph -v

Expected: fixture regressions pass within declared tolerances and repeated calls are identical.

- [ ] **Step 5: Commit**

    git add backend/algorithms tests/fixtures/dynagraph_cases.json tests/test_dynagraph.py
    git commit -m "feat: convert electric cards to dynagraphs"

### Task 4: Explainable Diagnosis and Production Estimation

**Files:**
- Create: backend/algorithms/diagnosis.py
- Create: backend/algorithms/production.py
- Create: tests/test_diagnosis.py
- Create: tests/test_production.py

**Interfaces:**
- Produces: diagnose(features: DynagraphFeatures, templates: list[Template]) -> DiagnosisResult
- Produces: estimate_liquid(features: DynagraphFeatures, pump: PumpParams, correction: float) -> ProductionEstimate

- [ ] **Step 1: Write failing diagnosis tests**

    def test_known_cases_return_reason_and_confidence(self):
        result = diagnose(features_for("pump_leak"), templates())
        self.assertEqual(result.condition, "pump_leak")
        self.assertGreaterEqual(result.confidence, 0.75)
        self.assertGreater(len(result.rule_hits), 0)
        self.assertGreater(len(result.candidates), 1)

    def test_production_formula_exposes_intermediates(self):
        result = estimate_liquid(features(fillage=0.72), pump(diameter_mm=44,
                                 stroke_m=3.0, strokes_per_minute=4.0), 1.0)
        self.assertGreater(result.theoretical_m3_day, result.estimated_m3_day)
        self.assertAlmostEqual(result.estimated_m3_day,
                               result.theoretical_m3_day * result.fillage * result.pump_efficiency,
                               places=6)

- [ ] **Step 2: Run tests and verify failure**

    python -m unittest tests.test_diagnosis tests.test_production -v

Expected: missing diagnosis and production modules.

- [ ] **Step 3: Implement hybrid diagnosis and production calculation**

Implement rule hits for load drop, area loss, low fillage, top-edge tilt, bottom impact, and curve closure. Normalize features and compute weighted Euclidean distance to stored templates. Confidence combines rule agreement and template distance. Production uses pump cross-sectional area, effective stroke, strokes per day, fillage, pump efficiency, and correction coefficient; return every factor.

- [ ] **Step 4: Run focused tests**

    python -m unittest tests.test_diagnosis tests.test_production -v

Expected: normal, shortage, intermittent, pump-leak, and empty-pump fixtures pass.

- [ ] **Step 5: Commit**

    git add backend/algorithms/diagnosis.py backend/algorithms/production.py tests/test_diagnosis.py tests/test_production.py
    git commit -m "feat: add explainable well diagnosis"

### Task 5: Analysis Persistence and APIs

**Files:**
- Create: backend/services/analysis.py
- Create: tests/test_analysis_api.py
- Modify: backend/api.py
- Modify: backend/http_server.py

**Interfaces:**
- Produces: AnalysisService.run(dynagraph_id: int, actor_id: int) -> AnalysisBundle
- Produces: GET /api/dynagraphs, GET /api/dynagraphs/{id}
- Produces: POST /api/dynagraphs/{id}/analyze
- Produces: POST /api/diagnoses/{id}/review
- Produces: GET /api/model-runs

- [ ] **Step 1: Write failing end-to-end analysis test**

    def test_analyze_persists_traceable_bundle(self):
        response = client.post("/api/dynagraphs/1/analyze", {})
        self.assertEqual(response.status, 200)
        data = response.json()["data"]
        self.assertIn("converted_points", data)
        self.assertIn("features", data)
        self.assertIn("diagnosis", data)
        self.assertIn("production", data)
        self.assertEqual(data["model_run"]["model_version"], "dynagraph-2.0")
        self.assertEqual(db.scalar("SELECT count(*) FROM audit_logs WHERE action='analysis.run'"), 1)

- [ ] **Step 2: Run test and verify failure**

    python -m unittest tests.test_analysis_api -v

Expected: analysis route returns 404.

- [ ] **Step 3: Implement analysis transaction and review**

Run conversion, feature extraction, diagnosis, and production estimation in one transaction. Persist intermediate JSON and foreign keys to the raw message, device, well, template, and model run. Manual review requires reviewer condition, comment, and optimistic version; it never overwrites the machine result.

- [ ] **Step 4: Run acquisition and analysis suites**

    python -m unittest tests.test_simulator tests.test_acquisition tests.test_dynagraph tests.test_diagnosis tests.test_production tests.test_analysis_api -v

Expected: all tests pass.

- [ ] **Step 5: Commit**

    git add backend/services/analysis.py backend/api.py backend/http_server.py tests/test_analysis_api.py
    git commit -m "feat: expose traceable dynagraph analysis"
