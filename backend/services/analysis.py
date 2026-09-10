"""Transactional, traceable dynagraph analysis application service."""

from dataclasses import asdict, fields
import hashlib
import json
import re
import sqlite3
import uuid

from backend.algorithms.diagnosis import Template, diagnose
from backend.algorithms.dynagraph import (
    DynagraphFeatures,
    ElectricalSample,
    MechanismParams,
    convert_electric_card,
)
from backend.algorithms.production import PumpParams, estimate_liquid


ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
CONDITION_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
CONDITIONS = {"normal", "supply_shortage", "intermittent", "pump_leak", "empty_pump"}
FEATURE_NAMES = tuple(field.name for field in fields(DynagraphFeatures))


class AnalysisValidationError(ValueError):
    pass


class AnalysisNotFound(RuntimeError):
    pass


class AnalysisVersionConflict(RuntimeError):
    pass


# Versioned reference vectors are deliberately data, not branches in the
# analysis service.  They are inserted into diagnosis_templates on first use
# and every model run records the exact selected version and metric config.
REFERENCE_FEATURES = {
    "normal": (71.41517008486971, 2.869076483559816, 52.70749268163911,
               .7530953922727579, 48.92907528889939, 25.3778237982589,
               23.551251490640492, .032582550634035985, -.9151894298309363,
               0.0, 6),
    "supply_shortage": (65.79931353256788, 8.12662225472522, 43.26265363667341,
                        .6181456080270107, 46.09067204174401, 26.74655286050809,
                        19.344119181235918, -.1064166308546378,
                        -.7877613747537806, 0.0, 6),
    "pump_leak": (69.30963354624214, 5.3499540291047865, 44.86892401861058,
                  .6410963264511955, 46.56037173419431, 26.304405661723592,
                  20.25596607247072, -.16481871378379176, -1.1835301862112249,
                  0.0, 6),
    "empty_pump": (58.435924406356705, 13.6512084735471, 33.29458999759325,
                   .4757198841965171, 43.419780246456945, 28.567893539170736,
                   14.851886707286209, .012573442893526977, -.14104362257532294,
                   0.0, 6),
    "intermittent": (67.0, 7.0, 41.0, .585, 46.0, 27.0, 19.0, -.05, -.80, .14, 11),
}


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _loads(value, fallback=None):
    if value is None or value == "":
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _plain(value):
    """Convert nested frozen dataclasses and tuples to a JSON-safe object."""
    if hasattr(value, "__dataclass_fields__"):
        return {key: _plain(item) for key, item in asdict(value).items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    return value


class AnalysisService:
    """Run conversion, diagnosis and production estimation as one unit."""

    def __init__(self, database, audit):
        self.database = database
        self.audit = audit

    def run(self, dynagraph_id: str, actor_id: int):
        self._id(dynagraph_id, "dynagraph id")
        row = self.database.query_one("SELECT * FROM dynagraphs WHERE id=?", (dynagraph_id,))
        if row is None:
            raise AnalysisNotFound()
        payload = self._payload(row["sample_data"])
        samples = self._samples(payload)
        parameters = self._parameters(payload)
        converted = convert_electric_card(samples, parameters)
        templates = self._templates()
        diagnosis = diagnose(converted.features, templates)
        production = estimate_liquid(
            converted.features,
            PumpParams(
                diameter_mm=44.0,
                stroke_m=parameters.stroke_m,
                strokes_per_minute=parameters.strokes_per_minute,
                pump_efficiency=.82,
                fillage_reference_load_span_kn=parameters.full_load_span_kn,
            ),
            1.0,
        )

        model_run_id = "MR-" + uuid.uuid4().hex.upper()
        diagnosis_id = "DX-" + uuid.uuid4().hex.upper()
        converted_points = [_plain(item) for item in converted.points]
        intermediate = [_plain(item) for item in converted.intermediate]
        features = _plain(converted.features)
        energy = _plain(converted.energy)
        parameters_data = _plain(parameters)
        diagnosis_data = {
            "id": diagnosis_id,
            "condition": diagnosis.condition,
            "confidence": diagnosis.confidence,
            "rule_hits": _plain(diagnosis.rule_hits),
            "all_rule_hits": _plain(diagnosis.all_rule_hits),
            "candidates": _plain(diagnosis.candidates),
            "evidence": list(diagnosis.evidence),
            "selected_template_version": diagnosis.selected_template_version,
            "recommendation": diagnosis.recommendation,
            "rule_baseline_version": diagnosis.rule_baseline_version,
            "metric_scales": dict(diagnosis.metric_scales),
            "metric_weights": dict(diagnosis.metric_weights),
            "model_version": diagnosis.model_version,
            "review_version": 1,
        }
        production_data = _plain(production)
        output_snapshot = {
            "converted_points": converted_points,
            "intermediate": intermediate,
            "features": features,
            "energy": energy,
            "conversion_units": dict(converted.units),
            "conversion_warnings": list(converted.warnings),
            "diagnosis": diagnosis_data,
            "production": production_data,
        }
        input_snapshot = {
            "dynagraph_id": dynagraph_id,
            "well_id": row["well_id"],
            "device_id": row["device_id"],
            "raw_message_id": row["raw_message_id"],
            "captured_at": row["captured_at"],
            "electric_samples": [_plain(item) for item in converted.raw_samples],
        }
        warnings = [*converted.warnings, *production.warnings]

        try:
            with self.database.transaction(
                actor_id, "analysis.run", "dynagraph", dynagraph_id,
                source_type="algorithm", auto_audit=False,
            ) as conn:
                template_ids = self._ensure_templates(
                    conn, dict(diagnosis.metric_scales), dict(diagnosis.metric_weights)
                )
                template_id = template_ids[diagnosis.condition]
                conn.execute(
                    """INSERT INTO model_runs(
                           id,model_name,model_version,input_object_type,input_object_id,
                           input_snapshot,parameters_json,output_snapshot,warnings_json,
                           completed_at,status,actor_id,source_type
                       ) VALUES(?, 'electric_card_hybrid_analysis', ?, 'dynagraph', ?,
                                ?,?,?,?,strftime('%Y-%m-%dT%H:%M:%f+08:00','now','+8 hours'),
                                'completed',?,'algorithm')""",
                    (
                        model_run_id, converted.model_version, dynagraph_id,
                        _json(input_snapshot), _json(parameters_data),
                        _json(output_snapshot), _json(warnings), actor_id,
                    ),
                )
                for feature_name, feature_value in features.items():
                    conn.execute(
                        """INSERT INTO dynagraph_features(
                               dynagraph_id,feature_name,feature_value,status,actor_id,source_type
                           ) VALUES(?,?,?,'valid',?,'algorithm')
                           ON CONFLICT(dynagraph_id,feature_name) DO UPDATE SET
                               feature_value=excluded.feature_value,status='valid',
                               actor_id=excluded.actor_id,source_type='algorithm'""",
                        (dynagraph_id, feature_name, feature_value, actor_id),
                    )
                conn.execute(
                    """INSERT INTO diagnoses(
                           id,well_id,dynagraph_id,model_run_id,template_id,template_version,
                           diagnosis_type,confidence,evidence,candidates_json,rule_hits_json,
                           recommendation,review_version,status,actor_id,source_type
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,'new',?,'algorithm')""",
                    (
                        diagnosis_id, row["well_id"], dynagraph_id, model_run_id,
                        template_id, diagnosis.selected_template_version,
                        diagnosis.condition, diagnosis.confidence,
                        _json(diagnosis_data["evidence"]),
                        _json(diagnosis_data["candidates"]),
                        _json(diagnosis_data["all_rule_hits"]),
                        diagnosis.recommendation, actor_id,
                    ),
                )
                cursor = conn.execute(
                    """INSERT INTO production_estimates(
                           well_id,dynagraph_id,model_run_id,theoretical_m3_day,
                           estimated_m3_day,factors_json,status,actor_id,source_type
                       ) VALUES(?,?,?,?,?,?,'estimated',?,'algorithm')""",
                    (
                        row["well_id"], dynagraph_id, model_run_id,
                        production.theoretical_m3_day, production.estimated_m3_day,
                        _json(production_data), actor_id,
                    ),
                )
                conn.execute(
                    """UPDATE dynagraphs SET conversion_json=?,features_json=?,
                           model_version=?,latest_model_run_id=?,status='analyzed',
                           actor_id=?,source_type='algorithm',version=version+1 WHERE id=?""",
                    (
                        _json({"points": converted_points, "intermediate": intermediate,
                               "energy": energy, "warnings": list(converted.warnings),
                               "units": dict(converted.units)}),
                        _json(features), converted.model_version, model_run_id,
                        actor_id, dynagraph_id,
                    ),
                )
                self.audit.record(
                    actor_id, "analysis.run", "dynagraph", dynagraph_id,
                    {
                        "before": {"status": row["status"], "latest_model_run_id": row["latest_model_run_id"]},
                        "after": {"status": "analyzed", "model_run_id": model_run_id,
                                  "diagnosis_id": diagnosis_id,
                                  "condition": diagnosis.condition,
                                  "production_estimate_id": cursor.lastrowid},
                    },
                    conn, source_type="algorithm",
                )
        except sqlite3.IntegrityError as error:
            raise AnalysisValidationError(f"analysis persistence rejected: {error}") from None

        return {
            "dynagraph_id": dynagraph_id,
            "converted_points": converted_points,
            "intermediate": intermediate,
            "features": features,
            "parameters": parameters_data,
            "energy": energy,
            "warnings": warnings,
            "model_run": {
                "id": model_run_id,
                "model_name": "electric_card_hybrid_analysis",
                "model_version": converted.model_version,
                "status": "completed",
            },
            "diagnosis": diagnosis_data,
            "production": production_data,
        }

    def list_dynagraphs(self, filters):
        filters = dict(filters or {})
        allowed = {"well_id", "status", "page", "page_size"}
        if set(filters) - allowed:
            raise AnalysisValidationError("unknown dynagraph filter")
        page = self._positive_int(filters.get("page", 1), "page", 1_000_000)
        page_size = self._positive_int(filters.get("page_size", 20), "page_size", 100)
        if (page - 1) * page_size > 9_223_372_036_854_775_807:
            raise AnalysisValidationError("page offset is out of range")
        clauses, values = ["1=1"], []
        if filters.get("well_id"):
            self._id(filters["well_id"], "well id")
            clauses.append("d.well_id=?")
            values.append(filters["well_id"])
        if filters.get("status"):
            if filters["status"] not in {"available", "unprocessed", "analyzed", "invalid"}:
                raise AnalysisValidationError("invalid dynagraph status")
            clauses.append("d.status=?")
            values.append(filters["status"])
        where = " AND ".join(clauses)
        total = self.database.scalar("SELECT COUNT(*) FROM dynagraphs d WHERE " + where, values)
        rows = self.database.query_all(
            """SELECT d.*,w.name AS well_name,dev.serial_number,
                      dx.diagnosis_type AS latest_condition,dx.confidence AS latest_confidence
               FROM dynagraphs d JOIN wells w ON w.id=d.well_id
               LEFT JOIN devices dev ON dev.id=d.device_id
               LEFT JOIN diagnoses dx ON dx.model_run_id=d.latest_model_run_id
               WHERE """ + where +
            " ORDER BY d.captured_at DESC,d.id DESC LIMIT ? OFFSET ?",
            [*values, page_size, (page - 1) * page_size],
        )
        return {
            "items": [self._dynagraph_summary(row) for row in rows],
            "page": page, "page_size": page_size, "total": total,
        }

    def detail(self, dynagraph_id):
        self._id(dynagraph_id, "dynagraph id")
        row = self.database.query_one(
            """SELECT d.*,w.name AS well_name,dev.serial_number
               FROM dynagraphs d JOIN wells w ON w.id=d.well_id
               LEFT JOIN devices dev ON dev.id=d.device_id WHERE d.id=?""",
            (dynagraph_id,),
        )
        if row is None:
            raise AnalysisNotFound()
        latest_diagnosis = self.database.query_one(
            "SELECT * FROM diagnoses WHERE model_run_id=?", (row["latest_model_run_id"],)
        ) if row["latest_model_run_id"] else None
        production = self.database.query_one(
            "SELECT * FROM production_estimates WHERE model_run_id=?", (row["latest_model_run_id"],)
        ) if row["latest_model_run_id"] else None
        model_run = self.database.query_one(
            "SELECT * FROM model_runs WHERE id=?", (row["latest_model_run_id"],)
        ) if row["latest_model_run_id"] else None
        reviews = []
        if latest_diagnosis:
            reviews = [self._review_item(item) for item in self.database.query_all(
                """SELECT r.*,u.display_name AS reviewer_name FROM diagnosis_reviews r
                   JOIN users u ON u.id=r.actor_id WHERE r.diagnosis_id=? ORDER BY r.id""",
                (latest_diagnosis["id"],),
            )]
        item = self._dynagraph_summary(row)
        item.update({
            "sample_data": _loads(row["sample_data"], {}),
            "conversion": _loads(row["conversion_json"]),
            "features": _loads(row["features_json"]),
            "latest_diagnosis": self._diagnosis_item(latest_diagnosis) if latest_diagnosis else None,
            "latest_production": self._production_item(production) if production else None,
            "latest_model_run": self._model_run_item(model_run) if model_run else None,
            "reviews": reviews,
        })
        return item

    def model_runs(self, filters):
        filters = dict(filters or {})
        allowed = {"dynagraph_id", "status", "page", "page_size"}
        if set(filters) - allowed:
            raise AnalysisValidationError("unknown model-run filter")
        page = self._positive_int(filters.get("page", 1), "page", 1_000_000)
        page_size = self._positive_int(filters.get("page_size", 20), "page_size", 100)
        if (page - 1) * page_size > 9_223_372_036_854_775_807:
            raise AnalysisValidationError("page offset is out of range")
        clauses, values = ["input_object_type='dynagraph'"], []
        if filters.get("dynagraph_id"):
            self._id(filters["dynagraph_id"], "dynagraph id")
            clauses.append("input_object_id=?")
            values.append(filters["dynagraph_id"])
        if filters.get("status"):
            if filters["status"] not in {"completed", "failed", "running"}:
                raise AnalysisValidationError("invalid model-run status")
            clauses.append("status=?")
            values.append(filters["status"])
        where = " AND ".join(clauses)
        total = self.database.scalar("SELECT COUNT(*) FROM model_runs WHERE " + where, values)
        rows = self.database.query_all(
            "SELECT * FROM model_runs WHERE " + where +
            " ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
            [*values, page_size, (page - 1) * page_size],
        )
        return {"items": [self._model_run_item(row) for row in rows],
                "page": page, "page_size": page_size, "total": total}

    def review(self, diagnosis_id, actor_id, condition, comment, version):
        self._id(diagnosis_id, "diagnosis id")
        if not isinstance(condition, str) or not CONDITION_PATTERN.fullmatch(condition) or condition not in CONDITIONS:
            raise AnalysisValidationError("condition is not a supported stable code")
        if not isinstance(comment, str) or not 2 <= len(comment.strip()) <= 500:
            raise AnalysisValidationError("comment must contain 2-500 characters")
        version = self._positive_int(version, "version", 2_147_483_647)
        with self.database.transaction(
            actor_id, "diagnosis.review", "diagnosis", diagnosis_id,
            source_type="manual", auto_audit=False,
        ) as conn:
            before = conn.execute("SELECT * FROM diagnoses WHERE id=?", (diagnosis_id,)).fetchone()
            if before is None:
                raise AnalysisNotFound()
            updated = conn.execute(
                "UPDATE diagnoses SET review_version=review_version+1,status='reviewed' "
                "WHERE id=? AND review_version=?", (diagnosis_id, version),
            )
            if updated.rowcount != 1:
                raise AnalysisVersionConflict()
            try:
                cursor = conn.execute(
                    """INSERT INTO diagnosis_reviews(
                           diagnosis_id,diagnosis_version,reviewer_condition,comment,
                           status,actor_id,source_type
                       ) VALUES(?,?,?,?,'recorded',?,'manual')""",
                    (diagnosis_id, version, condition, comment.strip(), actor_id),
                )
            except sqlite3.IntegrityError:
                raise AnalysisVersionConflict() from None
            review = conn.execute(
                """SELECT r.*,u.display_name AS reviewer_name FROM diagnosis_reviews r
                   JOIN users u ON u.id=r.actor_id WHERE r.id=?""", (cursor.lastrowid,)
            ).fetchone()
            self.audit.record(
                actor_id, "diagnosis.review", "diagnosis", diagnosis_id,
                {"before": {"review_version": version},
                 "after": {"review_version": version + 1,
                           "reviewer_condition": condition, "comment": comment.strip()},
                 "machine_result_unchanged": {"condition": before["diagnosis_type"],
                                              "confidence": before["confidence"]}},
                conn, source_type="manual",
            )
        return self._review_item(review)

    def _templates(self):
        return [
            Template(condition, DynagraphFeatures(*REFERENCE_FEATURES[condition]),
                     sample_count=48, version="reference-2026.1")
            for condition in ("normal", "supply_shortage", "pump_leak", "empty_pump", "intermittent")
        ]

    def _ensure_templates(self, conn, metric_scales, metric_weights):
        scales_json = _json(metric_scales)
        weights_json = _json(metric_weights)
        template_ids = {}
        for template in self._templates():
            version_digest = hashlib.sha256(template.version.encode("utf-8")).hexdigest()[:12].upper()
            generated_id = f"TPL-{template.condition.upper().replace('_', '-')}-{version_digest}"
            features_json = _json(_plain(template.features))
            conn.execute(
                """INSERT OR IGNORE INTO diagnosis_templates(
                       id,condition_code,version,features_json,scales_json,weights_json,
                       sample_count,status,source_type
                   ) VALUES(?,?,?,?,?,?,?,'active','algorithm')""",
                (
                    generated_id, template.condition, template.version, features_json,
                    scales_json, weights_json, template.sample_count,
                ),
            )
            persisted = conn.execute(
                """SELECT * FROM diagnosis_templates
                   WHERE condition_code=? AND version=?""",
                (template.condition, template.version),
            ).fetchone()
            expected = (
                features_json, scales_json, weights_json, template.sample_count, "active",
            )
            actual = (
                persisted["features_json"], persisted["scales_json"], persisted["weights_json"],
                persisted["sample_count"], persisted["status"],
            )
            if actual != expected:
                raise AnalysisValidationError(
                    f"diagnosis template {template.condition} does not match the active model contract"
                )
            template_ids[template.condition] = persisted["id"]
        return template_ids

    @staticmethod
    def _payload(text):
        payload = _loads(text)
        if not isinstance(payload, dict) or payload.get("kind") != "electric_card":
            raise AnalysisValidationError("dynagraph does not contain a validated electric card")
        return payload

    @staticmethod
    def _samples(payload):
        values = payload.get("electric_samples")
        if not isinstance(values, list):
            raise AnalysisValidationError("electric_samples are required")
        try:
            return [ElectricalSample(
                phase=item["phase"], current_a=item["current"],
                voltage_v=item["voltage"], active_power_kw=item["power"],
            ) for item in values]
        except (KeyError, TypeError):
            raise AnalysisValidationError("electric_samples are malformed") from None

    @staticmethod
    def _parameters(payload):
        mechanism = payload.get("mechanism")
        if not isinstance(mechanism, dict):
            raise AnalysisValidationError("mechanism parameters are required")
        try:
            stroke = mechanism["stroke_m"]
            return MechanismParams(
                stroke_m=stroke,
                strokes_per_minute=mechanism["strokes_per_minute"],
                motor_efficiency=.91,
                gear_ratio=30.0,
                transmission_efficiency=.86,
                balance_torque_nm=180.0,
                balance_phase_deg=8.0,
                torque_factor_m=stroke / 2.0,
                static_rod_load_kn=36.0,
                full_load_span_kn=25.0,
                dead_center_exclusion_deg=10.0,
            )
        except (KeyError, TypeError):
            raise AnalysisValidationError("mechanism parameters are malformed") from None

    @staticmethod
    def _dynagraph_summary(row):
        return {
            "id": row["id"], "well_id": row["well_id"], "well_name": row["well_name"],
            "device_id": row["device_id"], "device_serial_number": row["serial_number"],
            "raw_message_id": row["raw_message_id"], "captured_at": row["captured_at"],
            "status": row["status"], "model_version": row["model_version"],
            "latest_model_run_id": row["latest_model_run_id"], "version": row["version"],
            "latest_condition": row["latest_condition"] if "latest_condition" in row.keys() else None,
            "latest_confidence": row["latest_confidence"] if "latest_confidence" in row.keys() else None,
            "source_type": row["source_type"],
        }

    @staticmethod
    def _diagnosis_item(row):
        return {
            "id": row["id"], "condition": row["diagnosis_type"],
            "confidence": row["confidence"], "evidence": _loads(row["evidence"], []),
            "candidates": _loads(row["candidates_json"], []),
            "all_rule_hits": _loads(row["rule_hits_json"], []),
            "recommendation": row["recommendation"], "template_id": row["template_id"],
            "template_version": row["template_version"],
            "review_version": row["review_version"], "status": row["status"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _production_item(row):
        data = _loads(row["factors_json"], {})
        data.update({"id": row["id"], "model_run_id": row["model_run_id"],
                     "status": row["status"], "created_at": row["created_at"]})
        return data

    @staticmethod
    def _model_run_item(row):
        input_snapshot = _loads(row["input_snapshot"])
        output_snapshot = _loads(row["output_snapshot"])
        diagnosis = output_snapshot.get("diagnosis", {}) if isinstance(output_snapshot, dict) else {}
        production = output_snapshot.get("production", {}) if isinstance(output_snapshot, dict) else {}
        return {
            "id": row["id"], "model_name": row["model_name"],
            "model_version": row["model_version"],
            "input_object_type": row["input_object_type"],
            "input_object_id": row["input_object_id"], "status": row["status"],
            "parameters": _loads(row["parameters_json"]),
            "warnings": _loads(row["warnings_json"], []),
            "input_snapshot": input_snapshot,
            "output_snapshot": output_snapshot,
            "component_versions": {
                "dynagraph": row["model_version"],
                "diagnosis": diagnosis.get("model_version"),
                "production": production.get("model_version"),
                "template": diagnosis.get("selected_template_version"),
            },
            "completed_at": row["completed_at"], "created_at": row["created_at"],
        }

    @staticmethod
    def _review_item(row):
        return {
            "id": row["id"], "diagnosis_id": row["diagnosis_id"],
            "diagnosis_version": row["diagnosis_version"],
            "reviewer_condition": row["reviewer_condition"], "comment": row["comment"],
            "actor_id": row["actor_id"], "reviewer_name": row["reviewer_name"],
            "status": row["status"], "created_at": row["created_at"],
        }

    @staticmethod
    def _id(value, name):
        if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
            raise AnalysisValidationError(f"invalid {name}")
        return value

    @staticmethod
    def _positive_int(value, name, maximum):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > maximum:
            raise AnalysisValidationError(f"{name} is out of range")
        return value
