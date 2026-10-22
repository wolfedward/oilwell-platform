"""Fixed, fail-closed Arb arithmetic for finite-input v2 proofs only.

Arb endpoints are exported as exact dyadics, never through float or display
text. All users of flint in this process must hold proof_context throughout
construction, computation and endpoint export (flint.ctx is process-global).
"""
from contextlib import contextmanager
from dataclasses import dataclass
from fractions import Fraction as Q
from importlib import import_module
from importlib.metadata import version
import platform
import struct
import sysconfig
from threading import RLock, local
from types import MappingProxyType

from .transactions import InversionError

PRECISION = 128
MAX_BITS = 4096
MAX_EVALUATIONS = 524288
_LOCK = RLock()
_STATE = local()


def check_fraction(value: Q) -> Q:
    if type(value) is not Q:
        raise TypeError('Exact Fraction required')
    if max(value.numerator.bit_length(), value.denominator.bit_length()) > MAX_BITS:
        raise InversionError('proof_resource_limit')
    return value


@dataclass(frozen=True)
class RationalInterval:
    lo: Q
    hi: Q

    def __post_init__(self):
        check_fraction(self.lo)
        check_fraction(self.hi)
        if self.lo > self.hi:
            raise ValueError('Ordered outward endpoints required')

    @property
    def width(self):
        return check_fraction(self.hi - self.lo)


@dataclass
class ProofBudget:
    """Per-case counter. Call before each interval physical evaluation."""
    used: int = 0

    def __post_init__(self):
        if type(self.used) is not int or not 0 <= self.used <= MAX_EVALUATIONS:
            raise ValueError('Invalid proof counter')

    def consume(self, count=1):
        if type(count) is not int or count < 1:
            raise ValueError('Positive integer evaluation count required')
        if self.used + count > MAX_EVALUATIONS:
            raise InversionError('proof_resource_limit')
        self.used += count


def require_runtime():
    """Lazy native dependency: v1 imports/readers do not need Arb installed."""
    try:
        flint = import_module('flint')
        if (version('python-flint') != '0.9.0'
                or platform.python_implementation() != 'CPython'
                or platform.python_version() != '3.13.2'
                or platform.system() != 'Windows' or struct.calcsize('P') != 8
                or sysconfig.get_config_var('Py_GIL_DISABLED')
                or str(flint.__FLINT_VERSION__) != '3.6.0'):
            raise ValueError('Unapproved proof runtime')
        return MappingProxyType({'python_flint': '0.9.0', 'flint': '3.6.0',
                                 'python': '3.13.2', 'abi': 'cp313-cp313-win_amd64',
                                 'precision': PRECISION, 'threads': 1})
    except (ImportError, OSError, ValueError, AttributeError) as error:
        raise InversionError('proof_runtime_unavailable') from error


@contextmanager
def proof_context():
    with _LOCK:
        require_runtime()
        flint = import_module('flint')
        previous_threads = flint.ctx.threads
        previous_active = getattr(_STATE, 'active', False)
        try:
            flint.ctx.threads = 1
            with flint.ctx.workprec(PRECISION):
                _STATE.active = True
                yield flint
        finally:
            _STATE.active = previous_active
            flint.ctx.threads = previous_threads


def _active_module():
    if not getattr(_STATE, 'active', False):
        raise InversionError('proof_context_required')
    flint = import_module('flint')
    if flint.ctx.prec != PRECISION or flint.ctx.threads != 1:
        raise InversionError('proof_runtime_unavailable')
    return flint


def ball(value):
    """Convert only exact inputs, preserving the enclosing rounding radius."""
    flint = _active_module()
    if type(value) is int:
        value = Q(value)
    check_fraction(value)
    return flint.arb(flint.fmpq(value.numerator, value.denominator))


def _dyadic(endpoint):
    mantissa, exponent = map(int, endpoint.man_exp())
    # Check before shifting; a huge native exponent must not allocate a huge int.
    if exponent >= 0:
        if mantissa and mantissa.bit_length() + exponent > MAX_BITS:
            raise InversionError('proof_resource_limit')
        return check_fraction(Q(mantissa << exponent))
    if -exponent + 1 > MAX_BITS:
        raise InversionError('proof_resource_limit')
    return check_fraction(Q(mantissa, 1 << -exponent))


def enclose(value):
    flint = _active_module()
    if type(value) is not flint.arb:
        raise TypeError('Arb enclosure required')
    if not value.is_finite():
        raise InversionError('proof_nonfinite')
    return RationalInterval(_dyadic(value.lower()), _dyadic(value.upper()))
