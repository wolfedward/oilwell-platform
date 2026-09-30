"""Dataset-owned test helpers, including a non-resetting pinned connection."""
import os
from pathlib import Path
import tomllib
import psycopg
from backend.center.config import _postgres_settings
from backend.secret_store import read_windows_credential
from tools.provision_waveform_test import OUTPUT, PINNED_IDENTITY, load_identity, manifest_for, config_sections


def pinned_settings():
    path = Path(os.environ['OILWELL_WAVEFORM_TEST_CONFIG']).resolve()
    if path != (OUTPUT / 'waveform-test.local.toml').resolve(): raise ValueError('Unpinned waveform fixture')
    identity = load_identity(PINNED_IDENTITY)
    manifest = manifest_for(OUTPUT.resolve(), identity, database_exists=True)
    raw = tomllib.loads(path.read_text(encoding='utf-8'))
    if raw != config_sections(identity, manifest): raise ValueError('Modified waveform fixture configuration')
    return {role: _postgres_settings(raw['postgres_' + role], path, os.environ, read_windows_credential) | {'connect_timeout': 3}
            for role in manifest['roles']}


def pinned_connect(role='reader'):
    conn = psycopg.connect(**pinned_settings()[role])
    if conn.execute('SELECT current_database(),host(inet_server_addr()),inet_server_port()').fetchone() != ('oilwell_waveforms_test', '127.0.0.1', 3822):
        conn.close(); raise ValueError('Unpinned waveform endpoint')
    conn.rollback()
    return conn


from dataclasses import replace
from uuid import uuid4
from backend.waveforms.commands import Command
from tests.waveform_pairing_support import CREATOR, REVIEWER, seed_pair
from tests.waveform_support import WaveformDatabaseCase, LIMITS


class DatasetCase(WaveformDatabaseCase):
    def command(self,op,target=None,revision=0,actor=CREATOR,**values):
        from backend.waveforms.commands import dataset_request_hash
        command=Command(actor,uuid4(),revision,'Synthetic dataset test','0'*64)
        return replace(command,request_hash=dataset_request_hash(op,target,command,**values))

    def dataset_service(self):
        from backend.waveforms.datasets import DatasetService
        return DatasetService(lambda:self.connect('operator'),limits=LIMITS,shared_accounting_bucket='GW',read_connect=lambda:self.connect('reader'))

    def pair(self,*,two=False):
        from backend.waveforms.pairing import PairService
        from backend.waveforms.commands import pair_request_hash
        vector=seed_pair(self,two=two)
        service=PairService(lambda:self.connect('operator'),limits=LIMITS,shared_accounting_bucket='GW',read_connect=lambda:self.connect('reader'))
        body=dict(selection=vector['selection'],method_version='linear_adjacent/1')
        command=Command(CREATOR,uuid4(),0,'Synthetic dataset pair','0'*64)
        pair=service.propose(body,replace(command,request_hash=pair_request_hash('propose',None,command,body=body)))
        command=Command(REVIEWER,uuid4(),1,'Synthetic pair review','0'*64)
        service.review(pair.id,decision='approve',command=replace(command,request_hash=pair_request_hash('review',pair.id,command,decision='approve')))
        return pair,vector

    def submitted(self,pair,*,purpose='software_pair_test'):
        service=self.dataset_service(); body=dict(title='Synthetic collection',purpose=purpose,split_plan=dict(kind='none'))
        draft=service.create(body,self.command('create',body=body))
        body=dict(target_id=str(draft.id),changes=dict(upsert_members=[dict(kind='pair',reference_id=str(pair.id),quality_run_id=str(pair.left_quality_run_id),split='unassigned')]))
        draft=service.update(body,self.command('update',draft.id,draft.revision,body=body))
        body=dict(target_id=str(draft.id))
        draft=service.submit(body,self.command('submit',draft.id,draft.revision,body=body))
        return service,draft

    def frozen(self,*,two=False):
        from backend.waveforms.commands import Actor
        pair,vector=self.pair(two=two); service,draft=self.submitted(pair)
        body=dict(target_id=str(draft.id)); actor=Actor('local','dataset-reviewer','Dataset reviewer','admin')
        command=self.command('freeze',draft.id,draft.revision,actor,body=body)
        return service,service.freeze(body,command),vector
