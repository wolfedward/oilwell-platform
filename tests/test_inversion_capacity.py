from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
from tests.inversion_support import InversionDatabaseCase


class CapacityTests(InversionDatabaseCase):
    def test_accounting_scope_covers_retained_input_and_results_not_disk(self):
        import backend.inversion.capacity as capacity
        self.assertTrue(hasattr(capacity,'usage'),'unambiguous capacity scope required')
        with self.connect() as conn:
            with conn.cursor() as cur:
                reservation=capacity.reserve(cur,run_id=uuid4(),limits=self.limits)
                capacity.settle(cur,reservation,1024+2048)
                snapshot=capacity.usage(cur)
        self.assertEqual(snapshot['accounting_scope'],'run_input_and_results')
        self.assertEqual(snapshot['used_bytes'],3072)
        self.assertEqual(snapshot['reserved_bytes'],0)
        self.assertEqual(snapshot['total_storage_bytes'],self.limits.total_storage_bytes)

    def test_queue_and_byte_budget_race(self):
        from backend.inversion.capacity import reserve,claim,settle
        from backend.inversion.transactions import InversionError
        def admit(_):
            try:
                with self.connect() as conn:
                    with conn.cursor() as cur: return reserve(cur,run_id=uuid4(),limits=self.limits)
            except InversionError: return None
        with ThreadPoolExecutor(max_workers=4) as pool: reservations=list(filter(None,pool.map(admit,range(20))))
        self.assertEqual(len(reservations),16)
        def start(reservation):
            try:
                with self.connect() as conn:
                    with conn.cursor() as cur: claim(cur,reservation)
                return True
            except InversionError: return False
        with ThreadPoolExecutor(max_workers=4) as pool: self.assertEqual(sum(pool.map(start,reservations)),1)
        for reservation in reservations:
            with self.connect() as conn:
                with conn.cursor() as cur:
                    settle(cur,reservation,10); settle(cur,reservation,10)
        self.assertEqual(self.scalar('SELECT ROW(queued,running,reserved_bytes,used_bytes)::text FROM inversion.capacity'),'(0,0,0,160)')

    def test_durable_bytes_block_admission_and_bad_settlement_rolls_back(self):
        from backend.inversion.capacity import reserve,settle
        from backend.inversion.transactions import InversionError
        with self.connect('owner') as conn:
            conn.execute('UPDATE inversion.capacity SET used_bytes=%s',(self.limits.total_storage_bytes-80*1024**2,))
        with self.connect() as conn:
            with conn.cursor() as cur: reservation=reserve(cur,run_id=uuid4(),limits=self.limits)
        with self.connect() as conn:
            with conn.cursor() as cur:
                with self.assertRaises(InversionError): reserve(cur,run_id=uuid4(),limits=self.limits)
        with self.connect() as conn:
            with conn.cursor() as cur:
                with self.assertRaises(ValueError): settle(cur,reservation,-1)
        self.assertEqual(self.scalar('SELECT queued FROM inversion.capacity'),1)

    def test_concurrent_last_byte_budget_cannot_be_double_reserved(self):
        from backend.inversion.capacity import reserve,settle
        from backend.inversion.transactions import InversionError
        with self.connect('owner') as conn:
            conn.execute('UPDATE inversion.capacity SET used_bytes=%s',(self.limits.total_storage_bytes-80*1024**2,))
        def admit(_):
            try:
                with self.connect() as conn:
                    with conn.cursor() as cur: return reserve(cur,run_id=uuid4(),limits=self.limits)
            except InversionError: return None
        with ThreadPoolExecutor(max_workers=4) as pool: reservations=list(filter(None,pool.map(admit,range(4))))
        self.assertEqual(len(reservations),1)
        with self.connect() as conn:
            with conn.cursor() as cur: settle(cur,reservations[0],0)
        self.assertEqual(self.scalar('SELECT reserved_bytes FROM inversion.capacity'),0)

    def test_settlement_with_different_bytes_is_conflict_and_preserves_total(self):
        from backend.inversion.capacity import reserve,settle
        from backend.inversion.transactions import InversionError
        with self.connect() as conn:
            with conn.cursor() as cur:
                reservation=reserve(cur,run_id=uuid4(),limits=self.limits)
                settle(cur,reservation,123)
        with self.connect() as conn:
            with conn.cursor() as cur:
                with self.assertRaises(InversionError): settle(cur,reservation,0)
        self.assertEqual(self.scalar('SELECT used_bytes FROM inversion.capacity'),123)
