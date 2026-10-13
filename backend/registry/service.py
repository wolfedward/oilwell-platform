"""Explicit command dispatch; no transport, database setup or authorization side effects."""
from uuid import UUID
from functools import partial

from .contracts import Command
from .directory import apply_device, apply_well, lock_device, lock_well
from .errors import RegistryError
from .lifecycle import apply_lifecycle, lock_periods, lock_target_well
from .maps import apply_map, lock_map
from .transactions import RegistryTransactions


HANDLERS = {"well.create": apply_well, "well.change": apply_well,
            "device.create": apply_device, "device.change": apply_device,
            "map.create": apply_map, "map.review": apply_map,
            "device.bind": apply_lifecycle, "device.unbind": apply_lifecycle,
            "map.assign": apply_lifecycle, "device.state": apply_lifecycle}


def lock_references(cur, command: Command) -> None:
    # Acquired before the transaction's ONE database timestamp, including when
    # waiting for another connection. Handlers can then safely read these rows.
    if command.action == "well.change":
        lock_well(cur, command.target, command.body["expected_version"])
    elif command.action in HANDLERS and command.action not in {"well.create", "device.create"}:
        lock_device(cur, command.target, command.body["expected_version"])
        if command.action == "device.bind":
            lock_target_well(cur, UUID(command.body["well_id"]))
        elif command.action in {"map.review", "map.assign"}:
            lock_map(cur, command.target, command.body["map_version"])
        if HANDLERS[command.action] is apply_lifecycle:
            lock_periods(cur, command.target)


class RegistryService:
    def __init__(self, transactions: RegistryTransactions, *, require_independent_review=False):
        self.transactions = transactions
        self.require_independent_review = require_independent_review

    def execute(self, command: Command) -> dict:
        handler = HANDLERS.get(command.action)
        if handler is None:
            raise RegistryError("registry.invalid_action", "不支持的管理操作", 422)
        if command.action == "map.review" and self.require_independent_review:
            handler = partial(apply_map, require_independent_review=True)
        return self.transactions.run(command, handler, lock=lock_references)
