"""Transactional PostgreSQL repository for idempotent telemetry ingestion."""

from pathlib import Path


class PostgresRepositoryError(RuntimeError):
    pass


class IdempotencyConflict(PostgresRepositoryError):
    pass


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
        raw_text = bytes(raw_payload).decode("utf-8")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO ingest_messages
                       (message_id,topic,gateway_id,device_id,boot_id,sequence_first,sequence_last,
                        created_at,received_at,payload_sha256,raw_payload)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                       ON CONFLICT (message_id) DO NOTHING RETURNING message_id""",
                    (
                        batch.message_id, topic, batch.gateway_id, batch.device_id, batch.boot_id,
                        batch.samples[0].sequence, batch.samples[-1].sequence,
                        batch.created_at, received_at, payload_sha256, raw_text,
                    ),
                )
                if cursor.fetchone() is None:
                    cursor.execute(
                        "SELECT payload_sha256 FROM ingest_messages WHERE message_id=%s",
                        (batch.message_id,),
                    )
                    existing = cursor.fetchone()
                    if existing is None or existing[0] != payload_sha256:
                        raise IdempotencyConflict(
                            "message_id was reused with a different payload"
                        )
                    return False
                rows = [
                    (
                        batch.message_id, index, sample.gateway_id, sample.device_id,
                        sample.point_code, sample.measured_at, sample.sequence, sample.value,
                        sample.quality.value, sample.unit, sample.source, received_at,
                    )
                    for index, sample in enumerate(batch.samples)
                ]
                cursor.executemany(
                    """INSERT INTO telemetry_samples
                       (message_id,sample_index,gateway_id,device_id,point_code,measured_at,
                        sequence,value,quality,unit,source,received_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    rows,
                )
        return True
