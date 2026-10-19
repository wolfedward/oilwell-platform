"""Exclusive, bounded package I/O; do not accept links or unlisted members.

These local tools assume the owning user is not concurrently replacing directory
entries during I/O. They are not a multi-user privileged file-write service.
"""
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import re
import stat

from .config import MAX_BYTES, config_dict, parse_config
from .contracts import Case, PackConfig, PackIndex

SOURCE = {'kind': 'synthetic', 'description': '可复现软件测试输入，非现场测量'}
MANIFEST_FIELDS = {'schema', 'pack_id', 'profile', 'generator', 'configuration', 'source', 'cases', 'artifacts'}
RESERVED = {'manifest.json', 'manifest.sha256', '.incomplete'}
CATEGORIES = {'mechanistic', 'signal_fault', 'shape_only', 'protocol_fault'}
LAYERS = {'offline', 'numeric', 'software_network', 'http', 'browser'}


def _json_values(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError('non-finite JSON number')
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError('JSON object keys must be strings')
        for item in value.values():
            _json_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _json_values(item)


def canonical_bytes(value: object) -> bytes:
    _json_values(value)
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)+'\n').encode('utf-8')


def strict_json(raw: bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    def constant(value):
        raise ValueError('non-finite JSON constant')
    try:
        result = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)
        _json_values(result)
        return result
    except (UnicodeError, RecursionError) as exc:
        raise ValueError('invalid JSON') from exc


def derive_uniform(seed: int, case_id: str, channel: str, counter: int) -> float:
    if type(seed) is not int or not 0 <= seed < 2**32 or type(counter) is not int or counter < 0:
        raise ValueError('invalid random counter')
    integer = int.from_bytes(hashlib.sha256(canonical_bytes([seed, case_id, channel, counter])).digest()[:8], 'big')
    # binary64 rounding of UINT64_MAX would otherwise yield 1.0.
    return min(integer / 2**64, math.nextafter(1.0, 0.0))


def check_budget(sizes, limit: int) -> int:
    if type(limit) is not int or not 0 < limit <= MAX_BYTES:
        raise ValueError('invalid byte limit')
    total = 0
    for size in sizes:
        if type(size) is not int or size < 0 or size > limit-total:
            raise ValueError('package byte budget exceeded')
        total += size
    return total


def member_path(value: str, *, metadata=False) -> str:
    if not isinstance(value, str) or not value or len(value) > 240:
        raise ValueError('invalid member path')
    parts = value.split('/')
    if any(part in ('', '.', '..') or part[-1:] in ('.', ' ') or
           re.search(r'[\\:<>"|?*\x00-\x1f]', part) or
           re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?', part, re.I)
           for part in parts):
        raise ValueError('unsafe member path')
    if not metadata and value.casefold() in RESERVED:
        raise ValueError('reserved package member')
    return value


def safe_path(path: Path, *, missing=False) -> Path:
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('absolute non-traversing path required')
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            if missing and part == path:
                continue
            raise ValueError('missing path or parent') from None
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('links/reparse points forbidden')
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise ValueError('hard links forbidden')
    return path


def _generator(value):
    keys = {'version', 'source_commit', 'source_sha256', 'python_version', 'rng'}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError('generator fields')
    if (value['version'] != 'owp.testpack.generator/1' or value['rng'] != 'sha256-counter/1'
            or not re.fullmatch(r'[0-9a-f]{40,64}', str(value['source_commit']))
            or not re.fullmatch(r'[0-9a-f]{64}', str(value['source_sha256']))
            or not re.fullmatch(r'\d+\.\d+\.\d+', str(value['python_version']))):
        raise ValueError('generator fingerprint')


def pack_identity(config: PackConfig, generator: dict) -> str:
    _generator(generator)
    return 'tp-'+hashlib.sha256(canonical_bytes({'configuration': config_dict(config), 'generator': generator})).hexdigest()[:24]


def _case_record(value):
    if not isinstance(value, dict) or set(value) != {'case_id', 'category', 'facts', 'expected', 'artifact_paths'}:
        raise ValueError('case fields')
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,95}', str(value['case_id'])) or value['category'] not in CATEGORIES:
        raise ValueError('case identity/category')
    if not isinstance(value['facts'], dict) or not isinstance(value['expected'], (list, tuple)):
        raise ValueError('case facts/expected')
    for check in value['expected']:
        if not isinstance(check, dict) or set(check) != {'check_id', 'layer', 'assertion', 'target', 'expected'}:
            raise ValueError('expected check fields')
        if check['layer'] not in LAYERS or check['assertion'] not in {'equals', 'within', 'rejects', 'unchanged'}:
            raise ValueError('expected check is not a declarative assertion')
        if not isinstance(check['check_id'], str) or not isinstance(check['target'], str):
            raise ValueError('check identity/target')
    if not isinstance(value['artifact_paths'], (list, tuple)):
        raise ValueError('case artifact paths')
    for path in value['artifact_paths']:
        member_path(path)


def write_pack(target: Path, config: PackConfig, cases: tuple[Case, ...], generator: dict) -> PackIndex:
    target = safe_path(target, missing=True)
    if target.exists() or not target.parent.is_dir():
        raise ValueError('target must be new with existing parent')
    manifest = {'schema': 'owp.testpack/1', 'pack_id': pack_identity(config, generator),
                'profile': config.profile, 'generator': generator, 'configuration': config_dict(config),
                'source': SOURCE, 'cases': [], 'artifacts': []}
    artifacts, names, case_ids = [], set(), set()
    for case in cases:
        record = {key: getattr(case, key) for key in ('case_id', 'category', 'facts', 'expected')}
        record['artifact_paths'] = [a.path for a in case.artifacts]
        _case_record(record)
        if case.case_id in case_ids:
            raise ValueError('duplicate case ID')
        case_ids.add(case.case_id)
        manifest['cases'].append(record)
        for artifact in case.artifacts:
            name = member_path(artifact.path)
            if name.casefold() in names or not isinstance(artifact.content, bytes):
                raise ValueError('duplicate member or nonbytes content')
            names.add(name.casefold())
            artifacts.append(artifact)
            manifest['artifacts'].append({'path': name, 'media_type': artifact.media_type,
                'size_bytes': len(artifact.content), 'sha256': hashlib.sha256(artifact.content).hexdigest()})
    manifest['artifacts'].sort(key=lambda a: a['path'])
    manifest['cases'].sort(key=lambda c: c['case_id'])
    data = canonical_bytes(manifest)
    if len(data) > 4*1024*1024:
        raise ValueError('manifest too large')
    check_budget([len(data), 65, *(len(a.content) for a in artifacts)], config.max_bytes)
    target.mkdir()  # Exclusive ownership; failure never clears an existing directory.
    with (target/'.incomplete').open('xb') as out:
        out.write(b'Package write incomplete; retained for inspection.\n')
    for artifact in artifacts:
        dest = target/artifact.path
        dest.parent.mkdir(parents=True, exist_ok=True)
        safe_path(dest.parent)
        with dest.open('xb') as out:
            out.write(artifact.content)
    for name, content in (('manifest.json', data), ('manifest.sha256', (hashlib.sha256(data).hexdigest()+'\n').encode())):
        with (target/name).open('xb') as out:
            out.write(content)
    (target/'.incomplete').unlink()  # Only our marker in our newly created directory.
    return load_pack(target)


def _read_bounded(path, limit):
    safe_path(path)
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError('file byte limit exceeded')
    with path.open('rb') as stream:
        data = stream.read(limit+1)
    if len(data) > limit:
        raise ValueError('file grew beyond limit')
    return data


def load_pack(root: Path) -> PackIndex:
    root = safe_path(root)
    if not root.is_dir() or (root/'.incomplete').exists():
        raise ValueError('not a complete package')
    data = _read_bounded(root/'manifest.json', 4*1024*1024)
    digest = _read_bounded(root/'manifest.sha256', 65)
    if digest != (hashlib.sha256(data).hexdigest()+'\n').encode():
        raise ValueError('manifest digest mismatch')
    manifest = strict_json(data)
    if not isinstance(manifest, dict) or set(manifest) != MANIFEST_FIELDS or manifest['schema'] != 'owp.testpack/1':
        raise ValueError('manifest fields/schema')
    config = parse_config(manifest['configuration'])
    if (manifest['pack_id'] != pack_identity(config, manifest['generator'])
            or manifest['profile'] != config.profile or manifest['source'] != SOURCE):
        raise ValueError('manifest identity/source')
    if not isinstance(manifest['artifacts'], list) or not isinstance(manifest['cases'], list):
        raise ValueError('manifest collections')
    names, folded, sizes = set(), set(), [len(data), len(digest)]
    for item in manifest['artifacts']:
        if not isinstance(item, dict) or set(item) != {'path', 'media_type', 'size_bytes', 'sha256'}:
            raise ValueError('artifact fields')
        name = member_path(item['path'])
        if name.casefold() in folded or not isinstance(item['media_type'], str) or not re.fullmatch(r'[0-9a-f]{64}', str(item['sha256'])):
            raise ValueError('artifact identity/hash')
        names.add(name)
        folded.add(name.casefold())
        sizes.append(item['size_bytes'])
    check_budget(sizes, config.max_bytes)  # Before opening any payload.
    seen = set()
    for item in manifest['cases']:
        _case_record(item)
        if item['case_id'] in seen or any(name not in names for name in item['artifact_paths']):
            raise ValueError('case references')
        seen.add(item['case_id'])
    actual_files = set()
    for path in root.rglob('*'):
        safe_path(path)
        if path.is_file():
            actual_files.add(path.relative_to(root).as_posix())
    if actual_files != names | {'manifest.json', 'manifest.sha256'}:
        raise ValueError('unlisted or missing files')
    for item in manifest['artifacts']:
        path = root/item['path']
        safe_path(path)
        if path.stat().st_size != item['size_bytes']:
            raise ValueError('artifact size mismatch')
        digest, count = hashlib.sha256(), 0
        with path.open('rb') as stream:
            while block := stream.read(1024*1024):
                count += len(block)
                if count > item['size_bytes']:
                    raise ValueError('artifact grew')
                digest.update(block)
        if count != item['size_bytes'] or digest.hexdigest() != item['sha256']:
            raise ValueError('artifact digest mismatch')
    return PackIndex(root, manifest)
