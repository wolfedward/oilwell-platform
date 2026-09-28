"""PostgreSQL repositories for central telemetry ingestion and read models."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


class PostgresRepositoryError(RuntimeError):
    pass


class IdempotencyConflict(PostgresRepositoryError):
    pass


def insert_batch(cursor, batch, *, topic, payload_sha256, raw_payload, received_at) -> bool:
    """Insert into the caller's transaction; retain legacy unqualified tables.

    New callers set a safe search_path. Legacy session-local verification tables
    intentionally continue to resolve through their existing connection context.
    """
    raw_text = bytes(raw_payload).decode("utf-8")
    cursor.execute(
        """INSERT INTO ingest_messages
           (message_id,topic,gateway_id,device_id,boot_id,sequence_first,sequence_last,
            created_at,received_at,payload_sha256,raw_payload)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
           ON CONFLICT (message_id) DO NOTHING RETURNING message_id""",
        (batch.message_id, topic, batch.gateway_id, batch.device_id, batch.boot_id,
         batch.samples[0].sequence, batch.samples[-1].sequence,
         batch.created_at, received_at, payload_sha256, raw_text),
    )
    if cursor.fetchone() is None:
        cursor.execute("SELECT payload_sha256 FROM ingest_messages WHERE message_id=%s", (batch.message_id,))
        existing = cursor.fetchone()
        if existing is None or existing[0] != payload_sha256:
            raise IdempotencyConflict("message_id was reused with a different payload")
        return False
    rows = [(batch.message_id, index, sample.gateway_id, sample.device_id,
             sample.point_code, sample.measured_at, sample.sequence, sample.value,
             sample.quality.value, sample.unit, sample.source, received_at)
            for index, sample in enumerate(batch.samples)]
    cursor.executemany(
        """INSERT INTO telemetry_samples
           (message_id,sample_index,gateway_id,device_id,point_code,measured_at,
            sequence,value,quality,unit,source,received_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", rows)
    return True


class PostgresTelemetryRepository:
    def __init__(self, dsn: str | None = None, *, connect_kwargs=None, connect_factory=None):
        if not dsn and not connect_kwargs:
            raise ValueError("PostgreSQL connection settings are required")
        self.dsn = dsn
        self.connect_kwargs = dict(connect_kwargs or {})
        self._connect_factory = connect_factory

    def _connect(self):
        if self._connect_factory:
            return self._connect_factory(self.dsn, self.connect_kwargs)
        try:
            import psycopg
        except ImportError as error:
            raise PostgresRepositoryError(
                "psycopg is not installed; install requirements-production.txt"
            ) from error
        if self.dsn:
            return psycopg.connect(
                self.dsn,
                application_name="oilwell-center-ingestor",
                **self.connect_kwargs,
            )
        return psycopg.connect(
            application_name="oilwell-center-ingestor",
            **self.connect_kwargs,
        )

    def migrate(self):
        schema = (Path(__file__).with_name("schema.sql")).read_text(encoding="utf-8")
        with self._connect() as connection:
            connection.execute(schema)

    def store(self, batch, *, topic, payload_sha256, raw_payload, received_at) -> bool:
        # Preserve the legacy rejection boundary before any connection attempt.
        raw_payload = bytes(raw_payload)
        raw_payload.decode("utf-8")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                result = insert_batch(cursor, batch, topic=topic, payload_sha256=payload_sha256,
                                      raw_payload=raw_payload, received_at=received_at)
        return result


class PostgresTelemetryQueryRepository:
    """Read-only production views over the partitioned telemetry store."""

    def __init__(self, dsn: str | None = None, *, connect_kwargs=None, connect_factory=None):
        if not dsn and not connect_kwargs:
            raise ValueError("PostgreSQL connection settings are required")
        self.dsn = dsn
        self.connect_kwargs = dict(connect_kwargs or {})
        self._connect_factory = connect_factory

    def _connect(self):
        if self._connect_factory:
            return self._connect_factory(self.dsn, self.connect_kwargs)
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as error:
            raise PostgresRepositoryError(
                "psycopg is not installed; install requirements-production.txt"
            ) from error
        options = {
            **self.connect_kwargs,
            "application_name": "oilwell-platform-query",
            "row_factory": dict_row,
            "connect_timeout": 3,
        }
        return psycopg.connect(self.dsn, **options) if self.dsn else psycopg.connect(**options)

    @contextmanager
    def _query_connection(self):
        try:
            with self._connect() as connection:
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                connection.execute("SET LOCAL statement_timeout = '3000ms'")
                connection.execute("SET LOCAL lock_timeout = '1000ms'")
                connection.execute("SET LOCAL idle_in_transaction_session_timeout = '10000ms'")
                connection.execute("SET LOCAL timezone = 'UTC'")
                yield connection
        except PostgresRepositoryError:
            raise
        except Exception as error:
            raise PostgresRepositoryError("central PostgreSQL query failed") from error

    @staticmethod
    def _json_row(row):
        result = dict(row)
        for key, value in tuple(result.items()):
            if isinstance(value, datetime):
                result[key] = value.astimezone(timezone.utc).isoformat()
            elif key in {"sequence", "last_sequence"}:
                # JSON numbers are not exact BIGINT identifiers in browsers.
                result[key] = str(value)
        return result

    def list_samples(
        self,
        *,
        gateway_id=None,
        device_id=None,
        point_code=None,
        quality=None,
        from_time=None,
        to_time=None,
        received_before=None,
        page=1,
        page_size=50,
    ):
        clauses = []
        values = []
        for column, value in (
            ("gateway_id", gateway_id),
            ("device_id", device_id),
            ("point_code", point_code),
            ("quality", quality),
            ("measured_at >=", from_time),
            ("measured_at <=", to_time),
            ("received_at <=", received_before),
        ):
            if value is None:
                continue
            if column.endswith(" >=") or column.endswith(" <="):
                clauses.append(f"{column} %s")
            else:
                clauses.append(f"{column} = %s")
            values.append(value)
        where = " AND ".join(clauses) if clauses else "TRUE"
        offset = (page - 1) * page_size
        with self._query_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT COUNT(*) FROM telemetry_samples WHERE {where}",
                    tuple(values),
                )
                total_row = cursor.fetchone()
                total = int(total_row[0] if not isinstance(total_row, dict) else next(iter(total_row.values())))
                cursor.execute(
                    f"""SELECT message_id,sample_index,gateway_id,device_id,point_code,
                               measured_at,sequence,value,quality,unit,source,received_at
                          FROM telemetry_samples
                         WHERE {where}
                         ORDER BY measured_at DESC,message_id DESC,sample_index DESC
                         LIMIT %s OFFSET %s""",
                    (*values, page_size, offset),
                )
                items = [self._json_row(row) for row in cursor.fetchall()]
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    def health(self, *, now, stale_after_seconds=120):
        cutoff = now - timedelta(hours=24)
        with self._query_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT COUNT(DISTINCT gateway_id) AS gateway_count,
                              COUNT(DISTINCT device_id) AS device_count,
                              COUNT(DISTINCT point_code) AS point_count,
                              COUNT(*) AS samples_24h,
                              COUNT(*) FILTER (WHERE quality='bad') AS bad_samples_24h,
                              COUNT(*) FILTER (WHERE quality='uncertain') AS uncertain_samples_24h,
                              COUNT(*) FILTER (WHERE measured_at > received_at + INTERVAL '120 seconds') AS clock_ahead_samples_24h,
                              percentile_cont(0.95) WITHIN GROUP (
                                  ORDER BY EXTRACT(EPOCH FROM (received_at-measured_at))*1000
                              ) FILTER (WHERE measured_at <= received_at) AS p95_delivery_ms
                         FROM telemetry_samples
                        WHERE received_at >= %s AND received_at <= %s""",
                    (cutoff, now),
                )
                stats = dict(cursor.fetchone() or {})
                cursor.execute(
                    """WITH observed AS (
                           SELECT DISTINCT s.gateway_id,s.device_id,m.boot_id,s.sequence
                             FROM telemetry_samples s
                             JOIN ingest_messages m ON m.message_id=s.message_id
                            WHERE s.received_at >= %s AND s.received_at <= %s
                       ), ordered AS (
                           SELECT gateway_id,device_id,boot_id,sequence AS sequence_first,
                                  LAG(sequence) OVER (
                                      PARTITION BY gateway_id,device_id,boot_id
                                      ORDER BY sequence
                                  ) AS previous_last
                             FROM observed
                       )
                       SELECT COUNT(*) FILTER (
                                  WHERE previous_last IS NOT NULL
                                    AND sequence_first > previous_last + 1
                              ) AS gap_count,
                              COALESCE(SUM(
                                  CASE WHEN previous_last IS NOT NULL
                                         AND sequence_first > previous_last + 1
                                       THEN sequence_first - previous_last - 1 ELSE 0 END
                              ),0) AS missing_sequences
                         FROM ordered""",
                    (cutoff, now),
                )
                gaps = dict(cursor.fetchone() or {})
                cursor.execute(
                    """WITH latest AS (
                           SELECT DISTINCT ON (gateway_id,device_id)
                                  gateway_id,device_id,boot_id,sequence_last,
                                  received_at AS last_received_at
                             FROM ingest_messages
                            ORDER BY gateway_id,device_id,received_at DESC,message_id DESC
                       ), measurement AS (
                           SELECT gateway_id,device_id,
                                  COALESCE(MAX(measured_at) FILTER (
                                      WHERE measured_at <= received_at + INTERVAL '120 seconds'
                                  ), MAX(measured_at)) AS last_measured_at
                             FROM telemetry_samples GROUP BY gateway_id,device_id
                       ), recent AS (
                           SELECT gateway_id,device_id,COUNT(*) AS sample_count_24h,
                                  COUNT(*) FILTER (WHERE quality='bad') AS bad_sample_count_24h
                             FROM telemetry_samples
                            WHERE received_at >= %s AND received_at <= %s
                            GROUP BY gateway_id,device_id
                       )
                       SELECT latest.gateway_id,latest.device_id,latest.boot_id,
                              latest.sequence_last AS last_sequence,measurement.last_measured_at,
                              latest.last_received_at,COALESCE(recent.sample_count_24h,0) AS sample_count_24h,
                              COALESCE(recent.bad_sample_count_24h,0) AS bad_sample_count_24h
                         FROM latest
                         LEFT JOIN measurement USING (gateway_id,device_id)
                         LEFT JOIN recent USING (gateway_id,device_id)
                        ORDER BY latest.gateway_id,latest.device_id""",
                    (cutoff, now),
                )
                link_rows = cursor.fetchall()

        links = []
        status_counts = {"online": 0, "delayed": 0, "offline": 0}
        for raw in link_rows:
            row = dict(raw)
            received_at = row["last_received_at"]
            age_seconds = max(0, int((now - received_at).total_seconds()))
            measured_at = row.get("last_measured_at")
            measurement_age = (now - measured_at).total_seconds() if measured_at else None
            if age_seconds > stale_after_seconds * 3:
                status = "offline"
            elif (age_seconds > stale_after_seconds or measurement_age is None
                  or measurement_age > stale_after_seconds or measurement_age < -120):
                status = "delayed"
            else:
                status = "online"
            status_counts[status] += 1
            row["age_seconds"] = age_seconds
            row["measurement_age_seconds"] = round(measurement_age, 2) if measurement_age is not None else None
            row["status"] = status
            links.append(self._json_row(row))

        summary = {
            "gateway_count": len({row["gateway_id"] for row in links}),
            "device_count": len(links),
            "point_count": int(stats.get("point_count") or 0),
            "samples_24h": int(stats.get("samples_24h") or 0),
            "bad_samples_24h": int(stats.get("bad_samples_24h") or 0),
            "uncertain_samples_24h": int(stats.get("uncertain_samples_24h") or 0),
            "clock_ahead_samples_24h": int(stats.get("clock_ahead_samples_24h") or 0),
            "p95_delivery_ms": (
                round(float(stats["p95_delivery_ms"]), 2)
                if stats.get("p95_delivery_ms") is not None else None
            ),
            "sequence_gap_count_24h": int(gaps.get("gap_count") or 0),
            "missing_sequences_24h": str(int(gaps.get("missing_sequences") or 0)),
            "online_devices": status_counts["online"],
            "delayed_devices": status_counts["delayed"],
            "offline_devices": status_counts["offline"],
        }
        return {
            "status": "available",
            "generated_at": now.isoformat(),
            "stale_after_seconds": stale_after_seconds,
            "summary": summary,
            "links": links,
        }
