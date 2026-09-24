"""Independent application receipt publisher entry point."""
import argparse
import json
import random
import time

from .config import load_reliability_config
from .errors import ReliabilityError


def _event(event, **fields):
    print(json.dumps({'event': event, **fields}, ensure_ascii=False, sort_keys=True), flush=True)


def _build_worker(settings):
    try:
        import psycopg
        from .outbox import OutboxRepository
        from .privileges import probe_role
        from .publisher import ReceiptPublisher
    except ImportError:
        raise ReliabilityError('ingest.dependency_missing', '请安装生产接入依赖后再启用回执发送') from None
    connect = lambda: psycopg.connect(**settings.postgres_publisher)
    probe_role(connect, 'publisher')
    try:
        with connect() as connection:
            connection.execute('SET TRANSACTION READ ONLY')
            connection.execute("SET LOCAL lock_timeout='1s'")
            connection.execute("SET LOCAL statement_timeout='3s'")
            row = connection.execute('SELECT total_bytes,quarantine_bytes FROM ingest_reliability.capacity WHERE singleton=1').fetchone()
            if row != (settings.limits.total_bytes, settings.limits.quarantine_bytes):
                raise ReliabilityError('ingest.capacity_mismatch', '配置容量与已迁移数据库容量不一致，未执行更改')
    except psycopg.Error:
        raise ReliabilityError('ingest.connection_unavailable', '发送器数据库或容量检查暂不可用') from None
    return ReceiptPublisher(OutboxRepository(connect, retry_max_seconds=settings.retry_max_seconds),
                            settings.mqtt_publisher, batch_size=settings.publish_batch_size,
                            lease_seconds=settings.lease_seconds)


def run_loop(worker, *, once=False, retry_max_seconds=300, health_stale_seconds=120):
    failures = 0
    try:
        while True:
            try:
                result = worker.run_once()
                _event('publisher_iteration', **result)
                failed = result['failed'] > 0
            except ReliabilityError as error:
                # Never log driver messages, payloads, configuration or secrets.
                _event('publisher_failed', code=error.code)
                failed = True
            if once:
                return 1 if failed else 0
            if failed:
                failures = min(failures + 1, 12)
                maximum = min(retry_max_seconds, health_stale_seconds / 2, 2 ** failures)
                time.sleep(random.uniform(maximum / 2, maximum))
            else:
                failures = 0
                time.sleep(1)
    except KeyboardInterrupt:
        _event('publisher_stopped')
        return 0
    finally:
        worker.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Oil-well independent durable receipt publisher')
    parser.add_argument('--config', required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='Read-only schema/role/configuration checks; no MQTT connection')
    mode.add_argument('--once', action='store_true', help='Attempt one bounded batch, keeping failed receipts durable')
    args = parser.parse_args(argv)
    try:
        settings = load_reliability_config(args.config, component='publisher')
        if settings is None:
            _event('publisher_disabled')
            return 0
        worker = _build_worker(settings)
    except ReliabilityError as error:
        _event('publisher_startup_failed', code=error.code)
        return 2
    if args.check:
        worker.close()
        _event('configuration_valid', component='receipt_publisher', network_checked=False)
        return 0
    return run_loop(worker, once=args.once, retry_max_seconds=settings.retry_max_seconds,
                    health_stale_seconds=settings.health_stale_seconds)


if __name__ == '__main__':
    raise SystemExit(main())
