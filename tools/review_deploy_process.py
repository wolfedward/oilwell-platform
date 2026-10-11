"""Exact process ownership and loopback service lifecycle."""
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time


MOSQUITTO = Path('C:/Program Files/Mosquitto/mosquitto.exe')


def port_available(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE if os.name == 'nt' else socket.SO_REUSEADDR, 1)
        try:
            probe.bind(('127.0.0.1', port))
        except OSError:
            return False
    return True


def require_free_ports(ports):
    if set(ports) != {'http', 'postgres', 'mqtt'} or len(set(ports.values())) != 3 or any(
            type(value) is not int or not 1024 <= value <= 65535 for value in ports.values()):
        raise ValueError('three distinct nonprivileged ports required')
    if not all(port_available(value) for value in ports.values()):
        raise ValueError('requested loopback port is occupied')


def inspect_pid(pid):
    """Read one Windows PID's executable, command and creation time."""
    if type(pid) is not int or pid <= 0:
        return None
    command = ("[Console]::OutputEncoding=[Text.UTF8Encoding]::new();"
               f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}';"
               "if($p){$p | Select-Object ProcessId,ExecutablePath,CommandLine,CreationDate | ConvertTo-Json -Compress}")
    result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', command],
                            capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                            timeout=10)
    if result.returncode or not result.stdout.strip():
        return None
    try:
        raw = json.loads(result.stdout.decode('utf-8-sig'))
        return {'pid': int(raw['ProcessId']), 'created': str(raw['CreationDate']),
                'executable': str(raw['ExecutablePath']), 'command': str(raw['CommandLine'])}
    except (ValueError, KeyError, TypeError):
        return None


def owned_process(expected, actual):
    return actual is not None and all(expected.get(key) == actual.get(key) for key in
                                      ('pid', 'created', 'executable', 'command'))


def _record(pid):
    for _ in range(20):
        info = inspect_pid(pid)
        if info and info['executable'] and info['command']:
            return info
        time.sleep(0.1)
    raise RuntimeError('cannot attest launched process')


def _start(command, *, root, env, log_name):
    log_path = Path(root) / 'logs' / log_name
    with log_path.open('ab') as output:
        child = subprocess.Popen(command, cwd=Path(root) / 'code', env=env,
                                 stdout=output, stderr=subprocess.STDOUT,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        return _record(child.pid)
    except Exception:
        if child.poll() is None:
            child.terminate()  # the just-created process handle, never a guessed PID
            child.wait(timeout=5)
        raise


def _role_env(base, credentials, roles):
    from .review_deploy_config import env_name
    env = {key: value for key, value in base.items() if not key.startswith('REVIEW_')}
    for role in roles:
        env[env_name(role)] = credentials[role]
    env['PYTHONUTF8'] = '1'
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    return env


def start_services(root, *, ports, credentials, on_start=None):
    root = Path(root).absolute()
    from .review_deploy_config import env_name
    python = str(root / 'venv' / 'Scripts' / 'python.exe')
    code = root / 'code'
    private = root / 'private'
    base = os.environ.copy()
    processes = {}
    if not MOSQUITTO.is_file():
        raise RuntimeError('Mosquitto executable unavailable')
    processes['broker'] = _start([str(MOSQUITTO), '-c', str(private / 'mosquitto.conf')],
                                 root=root, env=_role_env(base, credentials, ()), log_name='broker.stdout.log')
    if on_start:
        on_start('broker', processes['broker'])
    specs = (
        ('center', ['-m', 'backend.center', '--config', str(private / 'center.toml')],
         ('mqtt_center', 'reliable_ingestor')),
        ('reliability_publisher', ['-m', 'backend.ingest_reliability', '--config', str(private / 'center.toml')],
         ('mqtt_receipts', 'reliable_publisher')),
        ('wave_receiver', ['-m', 'backend.waveforms', '--config', str(private / 'waveform.toml'), 'receive'],
         ('wave_receiver', 'mqtt_wave_receive')),
        ('wave_assembler', ['-m', 'backend.waveforms', '--config', str(private / 'waveform.toml'), 'assemble'],
         ('wave_assembler',)),
        ('wave_publisher', ['-m', 'backend.waveforms', '--config', str(private / 'waveform.toml'), 'publish'],
         ('wave_publisher', 'mqtt_wave_publish')),
        ('inversion', ['-m', 'backend.inversion', 'worker', '--config', str(private / 'inversion.toml')],
         ('inversion_reader', 'inversion_operator', 'wave_reader', 'wave_operator', 'wave_receiver')),
    )
    for name, arguments, roles in specs:
        processes[name] = _start([python, *arguments], root=root,
                                 env=_role_env(base, credentials, roles), log_name=name + '.log')
        if on_start:
            on_start(name, processes[name])
    if not port_available(ports['http']):
        raise ValueError('review HTTP port occupied')
    env = _role_env(base, credentials, ('mqtt_center', 'reliable_ingestor', 'foundation_reader', 'registry_writer',
        'reliable_reader', 'reliable_operator', 'wave_reader', 'wave_operator', 'wave_receiver',
        'inversion_reader', 'inversion_operator'))
    env.update(PLATFORM_DATABASE_PATH=str(private / 'identity.db'), PLATFORM_LOG_PATH=str(root / 'logs'),
               PLATFORM_NO_BROWSER='1')
    command = [python, str(code / 'server.py'), '--launch', '--runtime-mode=review', '--host', '127.0.0.1',
               '--port', str(ports['http']), '--center-config=' + str(private / 'center.toml'),
               '--waveform-config=' + str(private / 'waveform.toml'),
               '--inversion-config=' + str(private / 'inversion.toml')]
    with (root / 'logs' / 'http-launch.log').open('ab') as output:
        result = subprocess.run(command, cwd=code, env=env, stdout=output,
                                stderr=subprocess.STDOUT, timeout=90,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise RuntimeError('review HTTP launch failed; inspect private log')
    runtime = json.loads((root / 'logs' / 'runtime.json').read_text(encoding='utf-8'))
    if runtime.get('host') != '127.0.0.1' or runtime.get('port') != ports['http']:
        raise RuntimeError('review HTTP runtime identity mismatch')
    info = _record(runtime['pid'])
    if str(code / 'server.py').lower() not in info['command'].lower() or '--runtime-mode=review' not in info['command']:
        raise RuntimeError('review HTTP command identity mismatch')
    processes['http'] = info | {'instance_id': runtime['instance_id']}
    if on_start:
        on_start('http', processes['http'])
    return processes


def stop_services(root, processes):
    """Stop only exact recorded children; refuse PID reuse or command changes."""
    root = Path(root).absolute()
    for name in ('http', 'inversion', 'wave_publisher', 'wave_assembler', 'wave_receiver',
                 'reliability_publisher', 'center', 'broker'):
        expected = processes.get(name)
        if expected is None:
            continue
        actual = inspect_pid(expected['pid'])
        if actual is None:
            continue
        if not owned_process(expected, actual):
            raise ValueError(f'{name} process ownership mismatch')
        if name == 'http':
            runtime = json.loads((root / 'logs' / 'runtime.json').read_text(encoding='utf-8'))
            if runtime.get('pid') != expected['pid'] or runtime.get('instance_id') != expected['instance_id']:
                raise ValueError('HTTP runtime ownership mismatch')
            connection = http.client.HTTPConnection('127.0.0.1', int(runtime['port']), timeout=3)
            connection.request('POST', '/internal/shutdown', headers={
                'X-Platform-Shutdown-Token': runtime['shutdown_token']})
            response = connection.getresponse()
            response.read()
            connection.close()
            if response.status != 200:
                raise RuntimeError('owned HTTP refused shutdown')
        else:
            os.kill(expected['pid'], signal.SIGTERM)
        deadline = time.monotonic() + 30
        while inspect_pid(expected['pid']) is not None and time.monotonic() < deadline:
            time.sleep(0.2)
        if inspect_pid(expected['pid']) is not None:
            raise RuntimeError(f'owned {name} did not stop')
