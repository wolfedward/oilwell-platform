"""A member export must never acquire another member's original files."""
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

from backend.waveforms.commands import Command
from backend.waveforms.errors import WaveformError
from backend.waveforms.pair_contracts import capture_key
from backend.waveforms.member_exports import MemberResearchExporter,member_request_hash
from tests.waveform_dataset_support import DatasetCase
from tests.waveform_pairing_support import CREATOR
from tests.waveform_support import LIMITS


class MemberResearchTests(DatasetCase):
    def exporter(self):
        return MemberResearchExporter(lambda:self.connect('operator'),limits=LIMITS,
            shared_accounting_bucket='GW',read_connect=lambda:self.connect('reader'))

    def create_export(self, exporter, version, ordinal=0):
        command=Command(CREATOR,uuid4(),0,'Bounded member research','0'*64)
        command=replace(command,request_hash=member_request_hash(version,ordinal,command))
        return exporter.create(version,ordinal,command)

    def test_member_reads_only_selected_capture_and_audits_before_bytes(self):
        _,version,vector=self.frozen(two=True); exporter=self.exporter()
        with patch.object(exporter,'load_prepared',side_effect=AssertionError('whole dataset read')):
            record=self.create_export(exporter,version.id)
            for selected,wire in zip(vector['selection']['captures'],vector['wire_captures']):
                context=exporter.open(record.id,actor=CREATOR)
                iterator=context.stream_capture(capture_key(selected['key']))
                first=next(iterator)
                self.assertGreater(self.scalar("SELECT count(*) FROM waveforms.business_audit WHERE action='members.download'"),0)
                self.assertEqual(b''.join((first,*iterator)),b'\n'.join(wire)+b'\n')
                with self.assertRaises(WaveformError): list(context.stream_capture(capture_key(selected['key'])))
        self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.dataset_exports'),0)

    def test_member_export_actor_identity_and_membership(self):
        _,version,vector=self.frozen(); exporter=self.exporter()
        for connect in (exporter._reader,exporter._connect):
            with connect() as conn:
                self.assertEqual(conn.execute("SELECT current_setting('statement_timeout'),current_setting('lock_timeout')").fetchone(),('3s','1s'))
        record=self.create_export(exporter,version.id)
        renamed=replace(CREATOR,username='Renamed',role_code='admin')
        self.assertIsNotNone(exporter.open(record.id,actor=renamed))
        with self.assertRaises(WaveformError): exporter.open(record.id,actor=replace(CREATOR,user_id='other'))
        with self.assertRaises(WaveformError): self.create_export(exporter,version.id,999)

    def test_member_download_audit_capacity_failure_yields_no_bytes(self):
        import psycopg
        _,version,vector=self.frozen(); exporter=self.exporter()
        before=self.scalar("SELECT used_metadata_rows FROM waveforms.capacity_metadata WHERE bucket='GW'")
        record=self.create_export(exporter,version.id)
        self.assertEqual(self.scalar("SELECT used_metadata_rows FROM waveforms.capacity_metadata WHERE bucket='GW'")-before,3)
        with self.connect('migration_owner') as conn:
            old,used=conn.execute("SELECT metadata_rows,used_metadata_rows FROM waveforms.capacity WHERE bucket='GW'").fetchone()
            conn.execute("UPDATE waveforms.capacity SET metadata_rows=%s WHERE bucket='GW'",(used,))
        try:
            context=exporter.open(record.id,actor=CREATOR)
            with self.assertRaises((WaveformError,psycopg.Error)):
                next(context.stream_capture(capture_key(vector['selection']['captures'][0]['key'])))
            self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.business_audit WHERE action='members.download'"),0)
        finally:
            with self.connect('migration_owner') as conn:
                conn.execute("UPDATE waveforms.capacity SET metadata_rows=%s WHERE bucket='GW'",(old,))

    def test_member_is_bounded_with_1000_other_members(self):
        """Valid limit: selected+999; the 1000th other member is a separate version.

        Nonselected clones are metadata fixtures of one actual reviewed pair.
        The selected pair and all bytes it reads use the genuine WP04 lifecycle.
        """
        from tests.waveform_pairing_support import REVIEWER
        selected,vector=self.pair(two=True); other,_=self.pair()
        clones=[uuid4() for _ in range(999)]
        with self.connect('operator') as conn:
            for id in clones:
                conn.execute('INSERT INTO waveforms.pair_versions(pair_version_id,accounting_bucket,left_quality_run_id,right_quality_run_id,method_version,body,content_sha256,creator) SELECT %s,accounting_bucket,left_quality_run_id,right_quality_run_id,method_version,body,content_sha256,creator FROM waveforms.pair_versions WHERE pair_version_id=%s',(id,other.id))
                conn.execute('INSERT INTO waveforms.pair_reviews(pair_version_id,version,accounting_bucket,decision,actor,reason) SELECT %s,version,accounting_bucket,decision,actor,reason FROM waveforms.pair_reviews WHERE pair_version_id=%s',(id,other.id))
        service=self.dataset_service(); body=dict(title='Maximum bounded member fixture',purpose='software_pair_test',split_plan=dict(kind='none'))
        draft=service.create(body,self.command('create',body=body))
        members=[dict(kind='pair',reference_id=str(selected.id),quality_run_id=str(selected.left_quality_run_id),split='unassigned')]
        members.extend(dict(kind='pair',reference_id=str(id),quality_run_id=str(other.left_quality_run_id),split='unassigned') for id in clones)
        for start in range(0,1000,100):
            body=dict(target_id=str(draft.id),changes=dict(upsert_members=members[start:start+100]))
            draft=service.update(body,self.command('update',draft.id,draft.revision,body=body))
        body=dict(target_id=str(draft.id),changes=dict(upsert_members=[dict(kind='pair',reference_id=str(other.id),quality_run_id=str(other.left_quality_run_id),split='unassigned')]))
        with self.assertRaises(WaveformError): service.update(body,self.command('update',draft.id,draft.revision,body=body))
        body=dict(target_id=str(draft.id)); draft=service.submit(body,self.command('submit',draft.id,draft.revision,body=body))
        frozen=service.freeze(body,self.command('freeze',draft.id,draft.revision,REVIEWER,body=body))
        separate,extra=self.submitted(other); body=dict(target_id=str(extra.id))
        separate.freeze(body,self.command('freeze',extra.id,extra.revision,REVIEWER,body=body))
        self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.dataset_members'),1001)
        ordinal=self.scalar('SELECT ordinal FROM waveforms.dataset_members WHERE version_id=%s AND reference_id=%s',(frozen.id,selected.id))
        exporter=self.exporter(); read_ids=[]; raw=exporter._raw
        def monitored(id,actor,**kwargs):
            read_ids.append(id); return raw(id,actor,**kwargs)
        with patch.object(exporter,'load_prepared',side_effect=AssertionError('whole dataset read')),patch.object(exporter,'prepare',side_effect=AssertionError('unbounded preview')),patch.object(exporter,'_raw',side_effect=monitored):
            record=self.create_export(exporter,frozen.id,ordinal)
            for capture in vector['selection']['captures']:
                list(exporter.open(record.id,actor=CREATOR).stream_capture(capture_key(capture['key'])))
        with self.connect('reader') as conn:
            read_capture_ids=[r[0] for r in conn.execute('SELECT capture_id FROM waveforms.outcome_metadata WHERE fact_id=ANY(%s)',(read_ids,)).fetchall()]
        selected_capture_ids={capture_key(c['key']).capture_id for c in vector['selection']['captures']}
        self.assertEqual(set(read_capture_ids),selected_capture_ids)
        self.assertLessEqual(len(set(read_capture_ids)),2)
