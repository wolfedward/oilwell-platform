"""Pure transitions; persistence and receipt/lease identity are checked by the queue."""
from backend.ingest_reliability.errors import ReliabilityError


_STATES = {
    'application': ('pending', 'inflight', 'awaiting_receipt', 'stored', 'quarantined'),
    'broker': ('pending', 'inflight', 'broker_confirmed', 'legacy_broker_confirmed', 'dead'),
}
_EVENTS = ('claim', 'broker_ack', 'fail', 'exhausted', 'stored', 'quarantined')
_FINAL = ('stored', 'quarantined', 'broker_confirmed', 'legacy_broker_confirmed', 'dead')


def transition(current: str, mode: str, event: str) -> str:
    if (type(mode) is not str or mode not in _STATES
            or type(current) is not str or current not in _STATES[mode]
            or type(event) is not str or event not in _EVENTS):
        raise ReliabilityError('ingest.spool_state_invalid', 'Invalid queue transition.')
    if current in _FINAL:
        return current
    if event in ('stored', 'quarantined'):
        return event if mode == 'application' else current
    if event == 'claim':
        return 'inflight'
    if current == 'inflight':
        if event == 'broker_ack':
            return 'awaiting_receipt' if mode == 'application' else 'broker_confirmed'
        if event == 'exhausted' and mode == 'broker':
            return 'dead'
        if event in ('fail', 'exhausted'):
            return 'pending'
    return current
