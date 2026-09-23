"""Explicit command dispatch; no transport, database setup or authorization side effects."""
from .contracts import Command
from .directory import apply_device, apply_well, lock_device, lock_well
from .errors import RegistryError
from .transactions import RegistryTransactions


HANDLERS = {"well.create": apply_well, "well.change": apply_well,
            "device.create": apply_device, "device.change": apply_device}


def lock_references(cur, command: Command) -> None:
    # Acquired before the transaction's ONE database timestamp, including when
    # waiting for another connection. Handlers can then safely read these rows.
    if command.action == "well.change":
        lock_well(cur, command.target, command.body["expected_version"])
    elif command.action == "device.change":
        lock_device(cur, command.target, command.body["expected_version"])


class RegistryService:
    def __init__(self, transactions: RegistryTransactions):
        self.transactions = transactions

    def execute(self, command: Command) -> dict:
        handler = HANDLERS.get(command.action)
        if handler is None:
            raise RegistryError("registry.invalid_action", "不支持的管理操作", 422)
        return self.transactions.run(command, handler, lock=lock_references)
