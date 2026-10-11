"""Read-only MCU/DSP integration reference, NOT a protective device driver.

Use a private local directory. A checksum detects corruption, not malicious
administrator edits. Independent hardware protection is mandatory.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile

from backend.studies.protection import ProtectionReplay
from backend.studies.contracts import StudyError

MAX_CHECKPOINT_BYTES = 32768


class ReferenceStateError(ValueError):
    pass


def _text(value):
    if type(value) is not str or not value.strip() or len(value) > 240:
        raise ReferenceStateError('Explicit bounded local reference is required')
    return value


def _safe_path(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise ReferenceStateError('Checkpoint path may not traverse a link')
    if path.is_file() and path.stat().st_nlink != 1:
        raise ReferenceStateError('Checkpoint must not be a hard link')
    return path


def _bytes(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(raw) > MAX_CHECKPOINT_BYTES:
        raise ReferenceStateError('Checkpoint exceeds its bound')
    return raw


def _encoded(body):
    return _bytes({'body': body, 'sha256': hashlib.sha256(_bytes(body)).hexdigest()})


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReferenceStateError('Duplicate checkpoint key')
        result[key] = value
    return result


def _reject_constant(_):
    raise ReferenceStateError('Nonfinite checkpoint number')


class EdgeProtectionReference:
    """Exclusive local evidence adapter. Every exposed output remains disabled."""

    @staticmethod
    def initialize(root, settings, *, commissioning_reference):
        engine = ProtectionReplay(settings)
        reference = _text(commissioning_reference)
        root = _safe_path(root)
        if root.exists() or not root.parent.is_dir():
            raise ReferenceStateError('Initialization requires a new dedicated directory')
        root.mkdir(mode=0o700)
        body = {'schema': 'owp.edge.reference/1', 'commissioning_reference': reference,
                'engine': engine.snapshot(), 'notice': 'boot_self_check', 'local_confirmation': None}
        # Exclusive initialization; partial failure is retained, never adopted.
        with (root / 'checkpoint.json').open('xb') as handle:
            handle.write(_encoded(body))
            handle.flush()
            os.fsync(handle.fileno())

    def __init__(self, root, settings):
        self.root = _safe_path(root)
        self.engine = ProtectionReplay(settings)
        self._lock = None
        self._closed = False
        self.last_row = None
        self.notice = 'boot_self_check'
        self.startup_inhibit = True
        self.local_confirmation = None
        if not self.root.is_dir():
            raise ReferenceStateError('Explicit reference initialization is required')
        try:
            self._acquire()
            pending = _safe_path(self.root / 'pending.json')
            if pending.exists():
                raise ReferenceStateError('Uncertain prior write: local inspection required; no automatic repair')
            path = _safe_path(self.root / 'checkpoint.json')
            if not path.is_file() or path.stat().st_size > MAX_CHECKPOINT_BYTES:
                raise ReferenceStateError('Checkpoint unavailable or oversized')
            envelope = json.loads(path.read_bytes(), object_pairs_hook=_pairs, parse_constant=_reject_constant)
            if type(envelope) is not dict or set(envelope) != {'body', 'sha256'}:
                raise ReferenceStateError('Checkpoint envelope differs')
            body = envelope['body']
            if type(body) is not dict or set(body) != {'schema', 'commissioning_reference', 'engine', 'notice', 'local_confirmation'}:
                raise ReferenceStateError('Checkpoint fields differ')
            if envelope['sha256'] != hashlib.sha256(_bytes(body)).hexdigest() or body['schema'] != 'owp.edge.reference/1':
                raise ReferenceStateError('Checkpoint checksum or version differs')
            self.commissioning_reference = _text(body['commissioning_reference'])
            _text(body['notice'])
            if body['local_confirmation'] is not None:
                confirmation = body['local_confirmation']
                if type(confirmation) is not dict or set(confirmation) != {'actor', 'reason'}:
                    raise ReferenceStateError('Invalid local confirmation')
                _text(confirmation['actor']); _text(confirmation['reason'])
            self.engine.restore(body['engine'])
        except (OSError, ValueError, StudyError, RecursionError) as error:
            self.close()
            raise ReferenceStateError('Reference state unavailable; remain inhibited and retain artifacts') from error

    def _acquire(self):
        path = _safe_path(self.root / 'reference.lock')
        handle = path.open('a+b')
        try:
            if path.stat().st_size == 0:
                handle.write(b'0'); handle.flush()
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock = handle
        except OSError:
            handle.close()
            raise ReferenceStateError('Reference state already has an owner') from None

    def close(self):
        if self._lock is not None:
            self._lock.close()  # OS releases the exclusive byte/flock lock.
            self._lock = None
        self._closed = True

    def _available(self):
        if self._closed or self._lock is None:
            raise ReferenceStateError('Reference adapter is closed')
        if (self.root / 'pending.json').exists():
            self.startup_inhibit = True
            raise ReferenceStateError('Pending uncertain write requires local inspection')

    def _replace_checkpoint(self, raw):
        target = _safe_path(self.root / 'checkpoint.json')
        descriptor, temporary = tempfile.mkstemp(prefix='.checkpoint-', dir=self.root)
        temporary = Path(temporary)
        # On failure retain the owned temporary plus pending marker for inspection.
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(raw); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, target)

    def _persist(self):
        body = {'schema': 'owp.edge.reference/1', 'commissioning_reference': self.commissioning_reference,
                'engine': self.engine.snapshot(), 'notice': self.notice,
                'local_confirmation': self.local_confirmation}
        pending = _safe_path(self.root / 'pending.json')
        try:
            with pending.open('xb') as handle:
                handle.write(b'{"state":"uncommitted"}')
                handle.flush(); os.fsync(handle.fileno())
            self._replace_checkpoint(_encoded(body))
            _safe_path(pending).unlink()  # Only this adapter's exclusive journal.
        except (OSError, ValueError) as error:
            self.startup_inhibit = True
            self.notice = 'storage_uncertain'
            raise ReferenceStateError('Checkpoint write failed; remain inhibited') from error

    def receive(self, frame, *, local_reset=False):
        self._available()
        if type(local_reset) is not bool or type(frame) is not dict:
            raise ReferenceStateError('Invalid local frame')
        if frame.get('reset_request') == 'local' and not local_reset:
            raise ReferenceStateError('Network/unattested reset is not accepted')
        # Validate and calculate before changing accepted memory or filesystem.
        candidate = deepcopy(self.engine)
        try:
            row = candidate.step(frame)
        except StudyError as error:
            self.startup_inhibit = True
            self.notice = 'invalid_frame'
            self.last_row = None
            self.engine.restore(self.engine.snapshot())
            self._persist()
            raise ReferenceStateError('Invalid DSP evidence; remain inhibited') from error
        events = deepcopy(candidate.events)
        candidate.events.clear()  # Bounded adapter: consumer receives this step only.
        self.engine = candidate
        self.last_row = row
        if row['qualification'] != 'valid':
            self.startup_inhibit = True
        self.notice = 'recording' if row['qualification'] == 'valid' else 'input_unavailable'
        self._persist()
        return deepcopy(row) | {'events': events, 'control_enabled': False, 'hardware_execution': False}

    def confirm_local_startup(self, actor, reason):
        self._available()
        _text(actor); _text(reason)
        if self.last_row is None or self.last_row['qualification'] != 'valid' or self.last_row['inhibit']:
            raise ReferenceStateError('Fresh valid safe local evidence is required')
        if any(entry['state'] != 'normal' for entry in self.engine.final_state['rules'].values()):
            raise ReferenceStateError('Pickup or latched evidence cannot be cleared by confirmation')
        self.local_confirmation = {'actor': actor, 'reason': reason}
        self.startup_inhibit = False
        self.notice = 'local_shadow_ready'
        self._persist()
        return self.status()

    def mark_unavailable(self, reason):
        self._available()
        _text(reason)
        self.startup_inhibit = True
        self.notice = reason
        self.last_row = None
        self.engine.restore(self.engine.snapshot())
        self._persist()
        return self.status()

    def status(self):
        last_inhibit = self.last_row is None or self.last_row['inhibit']
        return {'schema': 'owp.edge.reference.status/1', 'settings_sha256': self.engine.settings_digest,
                'shadow_inhibit': self._closed or self.startup_inhibit or last_inhibit,
                'notice': self.notice, 'state': deepcopy(self.engine.final_state),
                'control_enabled': False, 'hardware_execution': False, 'field_validation': False}
