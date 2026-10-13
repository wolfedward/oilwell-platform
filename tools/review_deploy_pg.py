"""New isolated PostgreSQL cluster, role installation and ordered schema setup."""
import os
from pathlib import Path
import subprocess

import psycopg
from psycopg import sql

from .review_deploy_config import DATABASE, PG_OWNERS, pg_runtime, GATEWAY
from .review_deploy_paths import private_acl


PG_BIN = Path('C:/Program Files/PostgreSQL/18/bin')
STEPS = ('foundation_migrate', 'foundation_grants', 'registry_migrate', 'registry_grants',
         'reliability_migrate', 'reliability_grants', 'registry_pregrant',
         'waveform_migrate', 'waveform_grants', 'registry_history_grant',
         'inversion_migrate', 'inversion_grants', 'inversion_metadata_grant',
         'inversion_registry_grant')


def migration_steps():
    return STEPS


def run_migration_steps(operations):
    if set(operations) != set(STEPS):
        raise ValueError('incomplete migration plan')
    for name in STEPS:
        operations[name]()


def _binary(name):
    path = PG_BIN / (name + '.exe')
    if not path.is_file():
        raise RuntimeError(f'PostgreSQL binary unavailable: {name}')
    return str(path)


def _run(command, *, env=None, timeout=120):
    result = subprocess.run(command, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=timeout,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise RuntimeError('isolated PostgreSQL operation failed; inspect private log')


def connection_settings(port, role, password):
    return {'host': '127.0.0.1', 'port': port, 'dbname': DATABASE,
            'user': f'review_{role}', 'password': password,
            'sslmode': 'disable', 'connect_timeout': 3}


def initialize_cluster(root, *, port, credentials, profile='review'):
    """Create a fresh cluster and DB only inside the claimed target."""
    root = Path(root).absolute()
    data = root / 'postgres'
    if data.exists():
        raise ValueError('PostgreSQL data target already exists')
    password_file = root / 'private' / 'postgres-bootstrap.pw'
    if password_file.exists():
        raise ValueError('bootstrap password file already exists')
    with password_file.open('x', encoding='ascii') as stream:
        stream.write(credentials['bootstrap'] + '\n')
    private_acl(password_file)
    _run([_binary('initdb'), '-D', str(data), '--username=review_bootstrap',
          '--pwfile=' + str(password_file), '--auth-host=scram-sha-256',
          '--auth-local=scram-sha-256', '--encoding=UTF8', '--locale=C'], timeout=180)
    private_acl(data)
    start_cluster(root, port=port)
    bootstrap = connection_settings(port, 'bootstrap', credentials['bootstrap'])
    bootstrap['dbname'] = 'postgres'
    with psycopg.connect(**bootstrap, autocommit=True) as conn:
        for role in (*PG_OWNERS[1:], *pg_runtime(profile)):
            name = 'review_' + role
            conn.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS').format(
                sql.Identifier(name), sql.Literal(credentials[role])))
            if role == 'monitoring_reader':
                conn.execute('ALTER ROLE review_monitoring_reader NOINHERIT')
        conn.execute(sql.SQL('CREATE DATABASE {} OWNER {}').format(
            sql.Identifier(DATABASE), sql.Identifier('review_bootstrap')))
    with psycopg.connect(**connection_settings(port, 'bootstrap', credentials['bootstrap'])) as conn:
        conn.execute(sql.SQL('REVOKE ALL ON DATABASE {} FROM PUBLIC').format(sql.Identifier(DATABASE)))
        conn.execute('REVOKE ALL ON SCHEMA public FROM PUBLIC')
        for role in (*PG_OWNERS[1:], *pg_runtime(profile)):
            conn.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(
                sql.Identifier(DATABASE), sql.Identifier('review_' + role)))
            conn.execute(sql.SQL('GRANT USAGE ON SCHEMA public TO {}').format(sql.Identifier('review_' + role)))
    with psycopg.connect(**connection_settings(port, 'bootstrap', credentials['bootstrap'])) as conn:
        system_id = str(conn.execute('SELECT system_identifier FROM pg_control_system()').fetchone()[0])
    return system_id


def start_cluster(root, *, port):
    root = Path(root).absolute()
    _run([_binary('pg_ctl'), '-D', str(root / 'postgres'), '-l', str(root / 'logs' / 'postgres.log'),
          '-o', f'-h 127.0.0.1 -p {port}', '-w', 'start'])


def stop_cluster(root):
    root = Path(root).absolute()
    _run([_binary('pg_ctl'), '-D', str(root / 'postgres'), '-m', 'fast', '-w', 'stop'])


def cluster_running(root):
    root = Path(root).absolute()
    result = subprocess.run([_binary('pg_ctl'), '-D', str(root / 'postgres'), 'status'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return result.returncode == 0


def _owner_step(port, credentials, role, action):
    bootstrap = connection_settings(port, 'bootstrap', credentials['bootstrap'])
    with psycopg.connect(**bootstrap) as conn:
        conn.execute(sql.SQL('GRANT CREATE ON DATABASE {} TO {}').format(sql.Identifier(DATABASE), sql.Identifier('review_' + role)))
        if role == 'foundation_owner':
            conn.execute(sql.SQL('GRANT CREATE ON SCHEMA public TO {}').format(sql.Identifier('review_' + role)))
    try:
        return action(connection_settings(port, role, credentials[role]))
    finally:
        with psycopg.connect(**bootstrap) as conn:
            conn.execute(sql.SQL('REVOKE CREATE ON DATABASE {} FROM {}').format(sql.Identifier(DATABASE), sql.Identifier('review_' + role)))
            if role == 'foundation_owner':
                conn.execute(sql.SQL('REVOKE CREATE ON SCHEMA public FROM {}').format(sql.Identifier('review_' + role)))


def _with_role(port, credentials, role, action):
    with psycopg.connect(**connection_settings(port, role, credentials[role])) as conn:
        return action(conn)


def install_schemas(*, port, credentials, center_config, waveform_config, inversion_config, profile='review'):
    """Call authoritative migrations/grants with distinct new owners in fixed order."""
    from backend.center.postgres import PostgresTelemetryRepository
    from backend.registry import migrate as registry
    from backend.ingest_reliability import migrate as reliable
    from backend.ingest_reliability.contracts import CapacityLimits as ReliabilityLimits
    from backend.waveforms import migrate as waveform
    from backend.waveforms.registry_dependency import PROJECTIONS, grant_registry_history_prerequisites
    from backend.waveforms.config import load_waveform_config
    from backend.inversion import migrate as inversion
    from backend.inversion.capacity import InversionLimits
    from backend.inversion.metadata import grant_metadata, grant_registry_metadata
    from backend.inversion.config import load_inversion_config

    from .review_deploy_config import env_name, runtime_secrets
    previous = os.environ.copy()
    try:
        os.environ.update({env_name(role): credentials[role] for role in runtime_secrets(profile)})
        wave_cfg = load_waveform_config(waveform_config, component='management')
        inv_cfg = load_inversion_config(inversion_config)
    finally:
        os.environ.clear()
        os.environ.update(previous)
    reliability_limits = ReliabilityLimits(1073741824, 268435456)
    wave_roles = {'migration_owner': 'review_waveform_owner', 'receiver': 'review_wave_receiver',
                  'assembler': 'review_wave_assembler', 'receipt_publisher': 'review_wave_publisher',
                  'reader': 'review_wave_reader', 'operator': 'review_wave_operator'}
    inv_roles = {'owner': 'review_inversion_owner', 'reader': 'review_inversion_reader',
                 'operator': 'review_inversion_operator'}
    def pregrant(conn):
        # The reviewed post-migration helper requires both schemas, so the
        # initial dependency grant is deliberately narrow and separate.
        current, registry_schema, wave_schema = conn.execute(
            "SELECT current_user, to_regnamespace('device_registry'), to_regnamespace('waveforms')").fetchone()
        if current != 'review_registry_owner' or registry_schema is None or wave_schema is not None:
            raise ValueError('waveform schema exists before prerequisite grants')
        conn.execute(sql.SQL('GRANT USAGE ON SCHEMA device_registry TO {}').format(sql.Identifier('review_waveform_owner')))
        for table, columns in PROJECTIONS.values():
            owner = conn.execute('SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid=to_regclass(%s)',
                                 ('device_registry.' + table,)).fetchone()
            if owner != ('review_registry_owner',):
                raise ValueError('registry object ownership mismatch')
            conn.execute(sql.SQL('GRANT SELECT ({}) ON device_registry.{} TO {}').format(
                sql.SQL(',').join(map(sql.Identifier, columns)), sql.Identifier(table),
                sql.Identifier('review_waveform_owner')))
    def foundation_grants(conn):
        for role in ('foundation_reader', 'reliable_ingestor'):
            permissions = sql.SQL('SELECT') if role == 'foundation_reader' else sql.SQL('SELECT,INSERT')
            for table in ('ingest_messages', 'telemetry_samples'):
                conn.execute(sql.SQL('GRANT {} ON public.{} TO {}').format(
                    permissions, sql.Identifier(table), sql.Identifier('review_' + role)))
        # The independent reliability migration creates a foreign key into the
        # foundation schema. Grant only that column dependency, not ownership.
        conn.execute('GRANT REFERENCES (message_id) ON public.ingest_messages '
                     'TO review_reliability_owner')
    steps = {
        'foundation_migrate': lambda: _owner_step(port, credentials, 'foundation_owner',
            lambda settings: PostgresTelemetryRepository(connect_kwargs=settings).migrate()),
        'foundation_grants': lambda: _with_role(port, credentials, 'foundation_owner', foundation_grants),
        'registry_migrate': lambda: _owner_step(port, credentials, 'registry_owner',
            lambda settings: registry.migrate(settings, expected_database=DATABASE)),
        # These reviewed helpers grant across both the owned module schema and
        # public foundation tables; only the explicit initializer can do both.
        'registry_grants': lambda: _with_role(port, credentials, 'bootstrap',
            lambda conn: registry.grant_roles(conn, writer='review_registry_writer', reader='review_foundation_reader')),
        'reliability_migrate': lambda: _owner_step(port, credentials, 'reliability_owner',
            lambda settings: reliable.migrate(settings, expected_database=DATABASE, limits=reliability_limits)),
        'reliability_grants': lambda: _with_role(port, credentials, 'bootstrap',
            lambda conn: reliable.grant_roles(conn, roles={'ingestor': 'review_reliable_ingestor',
                'publisher': 'review_reliable_publisher', 'reader': 'review_reliable_reader',
                'operator': 'review_reliable_operator'})),
        'registry_pregrant': lambda: _with_role(port, credentials, 'registry_owner', pregrant),
        'waveform_migrate': lambda: _owner_step(port, credentials, 'waveform_owner',
            lambda settings: waveform.migrate(settings, expected_database=DATABASE, limits=wave_cfg.limits,
                shared_accounting_bucket=wave_cfg.shared_accounting_bucket)),
        'waveform_grants': lambda: _with_role(port, credentials, 'waveform_owner',
            lambda conn: waveform.grant_roles(conn, roles=wave_roles)),
        'registry_history_grant': lambda: _with_role(port, credentials, 'registry_owner',
            lambda conn: grant_registry_history_prerequisites(conn, registry_owner='review_registry_owner',
                waveform_owner='review_waveform_owner')),
        'inversion_migrate': lambda: _owner_step(port, credentials, 'inversion_owner',
            lambda settings: inversion.migrate(settings, expected_database=DATABASE,
                limits=InversionLimits(inv_cfg.total_storage_bytes))),
        'inversion_grants': lambda: _with_role(port, credentials, 'inversion_owner',
            lambda conn: inversion.grant_roles(conn, roles=inv_roles)),
        'inversion_metadata_grant': lambda: _with_role(port, credentials, 'waveform_owner',
            lambda conn: grant_metadata(conn, roles=inv_roles)),
        'inversion_registry_grant': lambda: _with_role(port, credentials, 'registry_owner',
            lambda conn: grant_registry_metadata(conn, roles=inv_roles)),
    }
    # Registry migration creates btree_gist IF NOT EXISTS. Bootstrap owns it,
    # satisfying the exact PG18 extension provenance check on runtime probes.
    _with_role(port, credentials, 'bootstrap', lambda conn: conn.execute('CREATE EXTENSION btree_gist WITH SCHEMA public'))
    run_migration_steps(steps)
    if profile == 'monitoring':
        from backend.monitoring.privileges import grant_reader, probe_reader
        for schema, owner in (('public', 'foundation_owner'), ('device_registry', 'registry_owner'),
                              ('ingest_reliability', 'reliability_owner')):
            _with_role(port, credentials, owner,
                       lambda conn, schema=schema: grant_reader(conn, role='review_monitoring_reader', schema=schema))
        probe_reader(lambda: psycopg.connect(**connection_settings(port, 'monitoring_reader', credentials['monitoring_reader'])))


def probe_system_identifier(port, credentials):
    with psycopg.connect(**connection_settings(port, 'bootstrap', credentials['bootstrap'])) as conn:
        return str(conn.execute('SELECT system_identifier FROM pg_control_system()').fetchone()[0])
