"""Only this test's spawned processes and temporary files; no database config."""
from datetime import datetime,timedelta,timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace as N
from uuid import uuid4

from backend.inversion.runs import Attempt,RunService
from backend.inversion.worker import WorkerServices

PARENTS={}


class Runs:
    def __init__(self):
        self.record=N(id=uuid4(),state='queued',reason_code=None,generation=1,owner_id=None,
            authority=None,actor=None,request=None,request_bytes=0)
        self.limits=N(deadline_seconds=180);self.settled=[]
    def claim(self,owner):
        self.record.state='running';self.record.owner_id=owner
        return Attempt(uuid4(),self.record.id,1,owner,datetime.now(timezone.utc)+timedelta(seconds=180))
    def get(self,_):return self.record
    def _authority(self,*_):return None
    def finish(self,attempt,state,reason):
        RunService._fence(self.record,attempt)
        self.settled.append((state,reason));self.record.state=state;self.record.reason_code=reason
        return self.record


class BlockedAcquisition:
    def __init__(self,body):self.body=body
    def prepare_input(self,*_,**__):
        child=subprocess.Popen([sys.executable,'-I','-c','import time; time.sleep(60)'],
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        (Path(self.body['root'])/'contained-ready.json').write_text(json.dumps({'nested_pid':child.pid}),encoding='utf-8')
        time.sleep(60)


def factory(configuration):
    body=json.loads(configuration)
    if os.getpid()==body['parent_pid']:return PARENTS[body['key']]
    runs=Runs();runs.record.state='running'
    return WorkerServices(runs,BlockedAcquisition(body))


def accelerated_clock():return time.monotonic()*60
