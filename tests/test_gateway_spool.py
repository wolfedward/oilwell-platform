"""Durable edge spool tests."""

from pathlib import Path
import tempfile
import unittest

from backend.gateway.spool import DurableSpool


class DurableSpoolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "uplink.db"

    def test_message_survives_restart_and_is_not_removed_before_ack(self):
        spool = DurableSpool(self.path)
        self.assertTrue(spool.enqueue("msg-1", "field/v1/G/D/telemetry", b"payload", now=0.0))
        self.assertFalse(spool.enqueue("msg-1", "field/v1/G/D/telemetry", b"payload", now=0.0))
        claimed = spool.claim(now=100.0, lease_seconds=30.0)
        self.assertEqual([(item.message_id, item.attempts) for item in claimed], [("msg-1", 1)])
        spool.close()

        reopened = DurableSpool(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.counts(), {"pending": 0, "inflight": 1, "acked": 0, "dead": 0})
        self.assertEqual(reopened.claim(now=120.0, lease_seconds=30.0), [])
        reclaimed = reopened.claim(now=131.0, lease_seconds=30.0)
        self.assertEqual(reclaimed[0].attempts, 2)
        self.assertTrue(reopened.ack("msg-1", now=132.0))
        self.assertEqual(reopened.counts()["acked"], 1)

    def test_failure_uses_bounded_exponential_backoff_and_dead_letters(self):
        spool = DurableSpool(self.path, max_attempts=3, base_backoff_seconds=2, max_backoff_seconds=5)
        self.addCleanup(spool.close)
        spool.enqueue("msg-1", "t", b"p", now=0.0)

        first = spool.claim(now=0.0)[0]
        spool.fail(first.message_id, "offline", now=0.0)
        self.assertEqual(spool.claim(now=1.99), [])
        second = spool.claim(now=2.0)[0]
        spool.fail(second.message_id, "offline", now=2.0)
        self.assertEqual(spool.claim(now=5.99), [])
        third = spool.claim(now=6.0)[0]
        spool.fail(third.message_id, "rejected", now=6.0)
        self.assertEqual(spool.counts()["dead"], 1)
        self.assertEqual(spool.claim(now=100.0), [])

    def test_only_matching_inflight_message_can_be_acknowledged(self):
        spool = DurableSpool(self.path)
        self.addCleanup(spool.close)
        spool.enqueue("msg-1", "t", b"p")
        self.assertFalse(spool.ack("msg-1"))
        spool.claim()
        self.assertTrue(spool.ack("msg-1"))
        self.assertFalse(spool.ack("missing"))


if __name__ == "__main__":
    unittest.main()
