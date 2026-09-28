"""Real loopback HTTP + SQLite authentication + complete isolated PostgreSQL DDL."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
import tempfile
import threading
from urllib.parse import urlencode
from uuid import uuid4

import psycopg

from backend.center.ingest import CentralIngestService
from backend.center.postgres import PostgresTelemetryRepository
from backend.gateway.contracts import TelemetrySample, UplinkBatch
from backend.http_server import create_server
from backend.registry.queries import RegistryQueries
from backend.registry.runtime import RegistryRuntime, probe_privileges
from backend.registry.service import RegistryService
from backend.registry.transactions import RegistryTransactions
from tests.registry_support import RegistryDatabaseCase


class RegistryEndToEndTests(RegistryDatabaseCase):
    def setUp(self):
        super().setUp()
        probe_privileges(lambda:self.connect("writer"),lambda:self.connect("reader"))
        self.runtime=RegistryRuntime("ready",RegistryService(RegistryTransactions(lambda:self.connect("writer"))),
            RegistryQueries(lambda:self.connect("reader"),online_seconds=120,delayed_seconds=900,clock_tolerance_seconds=300))
        self.temp=tempfile.TemporaryDirectory(prefix="registry-e2e-")
        self.addCleanup(self.temp.cleanup)
        self.server=create_server("127.0.0.1",0,Path(self.temp.name)/"auth.sqlite3",registry_runtime=self.runtime)
        self.origin=f"http://127.0.0.1:{self.server.server_port}"
        self.runtime=replace(self.runtime,allowed_origins=(self.origin,))
        self.server.registry_runtime=self.runtime
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.closed=False
        self.addCleanup(self.close_server)
        self.cookie=None
        status,login=self.http("POST","/api/auth/login",{"username":"administrator","password":"Admin@2026"})
        self.assertEqual(status,200)
        self.actor_id=str(self.server.auth.me(login["data"]["token"]).id)

    def close_server(self):
        if not self.closed:
            self.closed=True
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)

    def tearDown(self):
        if hasattr(self,"closed"):
            self.close_server()
        super().tearDown()

    def http(self,method,path,body=None):
        conn=http.client.HTTPConnection("127.0.0.1",self.server.server_port,timeout=8)
        headers={"Content-Type":"application/json","Origin":self.origin}
        if self.cookie:
            headers["Cookie"]=self.cookie
        try:
            conn.request(method,path,json.dumps(body,ensure_ascii=False).encode("utf-8") if body is not None else None,headers)
            response=conn.getresponse()
            if response.getheader("Set-Cookie"):
                self.cookie=response.getheader("Set-Cookie").split(";",1)[0]
            return response.status,json.loads(response.read())
        finally:
            conn.close()

    def write(self,path,**fields):
        status,payload=self.http("POST","/api/registry"+path,{"reason":"隔离环境跨层验证","idempotency_key":str(uuid4()),**fields})
        self.assertIn(status,(200,201),payload)
        result=payload["data"]
        self.assertIsInstance(result["registry_revision"],str)
        self.assertIsInstance(result["entity"]["version"],str)
        return result

    def get(self,path):
        status,payload=self.http("GET","/api/registry"+path)
        self.assertEqual(status,200,payload)
        return payload["data"]

    def ingest(self,*,measured=None,gateway="GW",device="D",quality="good"):
        measured=measured or datetime.now(timezone.utc)
        sample=TelemetrySample(gateway,device,"load",measured,9007199254740993,32.4,quality,"kN","isolated_test")
        batch=UplinkBatch.create(gateway,device,"isolated-boot",[sample])
        service=CentralIngestService(PostgresTelemetryRepository(connect_kwargs=self.settings["migrator"]))
        self.assertEqual(service.ingest(batch.topic("oilfield"),batch.to_json().encode("utf-8")).status,"stored")
        return batch.message_id

    def test_complete_http_lifecycle_keeps_late_historical_provenance_after_retirement(self):
        self.assertEqual(self.get("/devices")["items"],[])
        self.assertEqual(self.get("/wells")["items"],[])
        well=self.write("/wells",area_code="A",well_code="W1",name="测试井一")["entity"]
        other=self.write("/wells",area_code="A",well_code="W2",name="测试井二")["entity"]
        device=self.write("/devices",gateway_id="GW",device_id="D",name="联调终端",transport="mqtt_direct",usage="commissioning")["entity"]
        path="/devices/"+device["id"]
        self.assertEqual(self.get(path+"/link-status")["link_status"],"never_reported")
        def change(suffix,**fields):
            nonlocal device
            result=self.write(path+suffix,expected_version=device["version"],**fields)
            device=self.get(path)
            return result
        change("/bindings",well_id=well["id"])
        first=change("/point-maps",parent_version=None,points=[{"point_code":"load","unit":"kN","engineering_min":0,"engineering_max":80}])["entity"]
        second=change("/point-maps",parent_version=first["map_version"],points=[{"point_code":"load","unit":"kN","engineering_min":0,"engineering_max":90}])["entity"]
        self.assertEqual(second["map_version"],"2")
        change("/point-maps/2/review",decision="approved",sha256=second["sha256"])
        change("/point-map-assignment",map_version="2")
        activated=change("/state",status="active",check_note="已核对隔离测试登记和期望定义")
        original_time=datetime.fromisoformat(activated["committed_at"])+timedelta(microseconds=1)
        initial_message=self.ingest(measured=original_time)
        query="/resolved-telemetry?"+urlencode({"gateway_id":"GW","device_id":"D"})
        initial=self.get(query)["items"][0]
        self.assertEqual((initial["well_id"],initial["expected_map_version"],initial["sample_state"]),(well["id"],"2","active"))
        self.assertTrue(initial["matched"])
        self.assertFalse(initial["eligible_for_automation"])
        self.assertIsNone(initial["reported_map_version"])
        self.assertEqual(initial["application_status"],"unverified")
        change("/bindings",well_id=other["id"])
        self.assertEqual(device["status"],"suspended")
        self.assertTrue(all(item["valid_to"] is not None for item in self.get(path+"/point-map-assignments")["items"]))
        change("/point-map-assignment",map_version="2")
        change("/state",status="active",check_note="迁井后重新核对登记资料")
        change("/state",status="retired")
        late_message=self.ingest(measured=original_time)
        history=self.get(query)["items"]
        self.assertEqual(len(history),2)
        for item in history:
            self.assertEqual((item["well_id"],item["sample_state"],item["current_state"]),(well["id"],"active","retired"))
            self.assertEqual((item["value"],item["unit"],item["source"],item["sequence"]),(32.4,"kN","isolated_test","9007199254740993"))
        self.assertEqual({item["message_id"] for item in history},{initial_message,late_message})
        maps=self.get(path+"/point-maps")["items"]
        self.assertEqual([(item["version"],item["status"]) for item in maps],[("2","approved"),("1","draft")])
        audit=self.get("/audit?"+urlencode({"object_id":device["id"]}))["items"]
        self.assertGreater(len(audit),8)
        self.assertTrue(all(item["actor"]["user_id"]==self.actor_id for item in audit))
        self.assertEqual(self.scalar("SELECT count(*) FROM public.telemetry_samples"),2)

    def test_unknown_pair_still_ingests_and_is_not_silently_registered(self):
        message=self.ingest(gateway="UNKNOWN-GW",device="UNKNOWN-D")
        result=self.get("/resolved-telemetry?gateway_id=UNKNOWN-GW&device_id=UNKNOWN-D")["items"][0]
        self.assertEqual(result["message_id"],message)
        self.assertIn("not_registered",result["reasons"])
        self.assertFalse(result["eligible_for_automation"])
        self.assertEqual(self.get("/devices")["items"],[])

    def test_http_audit_failure_rolls_back_business_and_receipt(self):
        with self.connect("migrator") as conn:
            conn.execute("CREATE TRIGGER registry_test_reject_audit BEFORE INSERT ON device_registry.audit_events FOR EACH ROW EXECUTE FUNCTION device_registry.immutable_row()")
        try:
            status,_=self.http("POST","/api/registry/wells",{"area_code":"A","well_code":"W","name":"测试井","reason":"失败回滚验证","idempotency_key":str(uuid4())})
            self.assertEqual(status,503)
            for table in ("wells","audit_events","operation_receipts"):
                self.assertEqual(self.scalar("SELECT count(*) FROM device_registry."+table),0)
            self.assertEqual(self.scalar("SELECT revision FROM device_registry.registry_head"),1)
        finally:
            with self.connect("migrator") as conn:
                conn.execute("DROP TRIGGER registry_test_reject_audit ON device_registry.audit_events")

    def test_http_uncertain_commit_recovers_original_result_without_second_write(self):
        real=self.connect("writer")
        class LostAcknowledgement:
            def __getattr__(self,name): return getattr(real,name)
            def __enter__(self): real.__enter__(); return self
            def __exit__(self,kind,value,traceback):
                result=real.__exit__(kind,value,traceback)
                if kind is None: raise psycopg.OperationalError("lost reply after confirmed test commit")
                return result
        uncertain=replace(self.runtime,service=RegistryService(RegistryTransactions(lambda:LostAcknowledgement())))
        self.server.registry_runtime=uncertain
        body={"area_code":"A","well_code":"W","name":"测试井","reason":"回执重试验证","idempotency_key":str(uuid4())}
        self.assertEqual(self.http("POST","/api/registry/wells",body)[0],503)
        self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.wells"),1)
        self.server.registry_runtime=self.runtime
        status,result=self.http("POST","/api/registry/wells",body)
        self.assertEqual(status,200)
        self.assertTrue(result["data"]["replayed"])
        self.assertEqual(result["data"]["registry_revision"],"2")
        self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.operation_receipts"),1)
        with self.connect("reader") as conn:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                conn.execute("UPDATE device_registry.wells SET name='forbidden'")
