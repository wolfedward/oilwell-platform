"""Real outbox with controlled MQTT callback races and owned broker evidence."""
import importlib
import os
import threading
import time
from datetime import timedelta
from unittest.mock import patch
from types import SimpleNamespace
from tests.test_waveform_outbox import OutboxFixture
from tests.waveform_stack import runtime_config


class Client:
    def __init__(self, mode='early', old=None):
        self.mode,self.old=mode,old
        self._sockpairR=self._sockpairW=None
        self.sent=[]
    def connect(self,*args,**kwargs):
        self.connect_options=kwargs
        self.on_connect(self,None,None,0,None)
        return 0
    def socket(self): return None
    def _reset_sockets(self): pass
    def disconnect(self): return 0
    def loop(self,timeout=.2): return 0
    def publish(self,topic,payload,*,qos,retain):
        self.sent.append((topic,payload,qos,retain))
        if self.old: self.old(self,None,7,0,None)
        if self.mode=='early': self.on_publish(self,None,7,0,None)
        elif self.mode=='no_subscribers': self.on_publish(self,None,7,16,None)
        elif self.mode=='negative': self.on_publish(self,None,7,135,None)
        return SimpleNamespace(rc=0,mid=7)


class WaveformPublisherTests(OutboxFixture):
    def test_new_connection_does_not_inherit_disconnected_error(self):
        outbox=self.outbox(); receipt=self.receipt(); first=Client()
        def disconnected(*_args,**_kwargs):
            first.on_disconnect(first,None,None,0,None)
            return SimpleNamespace(rc=0,mid=7)
        first.publish=disconnected
        publisher=self.publisher(outbox,first)
        self.assertEqual(publisher.run_once(),0)
        self.now+=timedelta(seconds=2)
        second=Client(); publisher.client_factory=lambda _:second
        self.assertEqual(publisher.run_once(),1)
        self.assertEqual(self.state(receipt)[1],1)

    def test_local_deadline_prevents_network_publish(self):
        from dataclasses import replace
        outbox=self.outbox(); receipt=self.receipt(); client=Client()
        claim=outbox.claim
        def expired(*args,**kwargs):
            return tuple(replace(lease,_deadline=0) for lease in claim(*args,**kwargs))
        with patch.object(outbox,'claim',side_effect=expired):
            self.assertEqual(self.publisher(outbox,client).run_once(),0)
        self.assertEqual(client.sent,[])
        self.assertEqual(self.state(receipt)[1],0)

    def test_early_callback_buffer_overflow_fails_closed(self):
        outbox=self.outbox(); receipt=self.receipt(); client=Client()
        def flood(*_args,**_kwargs):
            for mid in range(1,66): client.on_publish(client,None,mid,0,None)
            return SimpleNamespace(rc=0,mid=7)
        client.publish=flood
        self.assertEqual(self.publisher(outbox,client).run_once(),0)
        self.assertEqual(self.state(receipt)[1],0)
        self.assertEqual(self.state(receipt)[4],'mqtt.callback_overflow')

    def test_close_during_puback_wait_does_not_finish(self):
        from concurrent.futures import ThreadPoolExecutor
        outbox=self.outbox(); receipt=self.receipt(); client=Client('lost'); entered=threading.Event()
        original=client.publish
        def send(*args,**kwargs):
            value=original(*args,**kwargs); entered.set(); return value
        client.publish=send
        publisher=self.publisher(outbox,client); publisher.ack_timeout=5
        with ThreadPoolExecutor(1) as pool:
            future=pool.submit(publisher.run_once)
            try: self.assertTrue(entered.wait(3))
            finally: publisher.close()
            self.assertEqual(future.result(timeout=3),0)
        self.assertEqual(self.state(receipt)[1],0)
        self.assertIsNotNone(self.state(receipt)[2])

    def test_crashed_owned_sender_replays_unfinished_receipt(self):
        from tests.waveform_stack import Stack
        from tests.ingest_broker import Peer
        receipt=self.receipt()
        with Stack(self) as stack:
            peer=Peer(stack.broker,'waveform-GW-sender'); stack.broker.clients.append(peer)
            peer.subscribe('oil/v2/GW/D/waveform-ack')
            process=stack.launch('publish',fault='pause_publish_finish')
            paused=stack.await_event('publish','publisher_finish_paused')
            first=peer.messages.get(timeout=3)
            self.assertEqual(paused['receipt_id'],str(receipt))
            self.assertEqual(self.state(receipt)[1],0)
            process.kill(); process.wait(timeout=5); stack.stop('publish')
            # Simulate expiry after this confirmed owned-process death, without
            # changing the cluster clock or waiting through the lease duration.
            with self.connect('migration_owner') as conn:
                conn.execute("UPDATE waveforms.deliveries SET lease_until=clock_timestamp()-interval '1 second' WHERE receipt_id=%s",(receipt,))
            stack.launch('publish')
            stack.await_event('publish','broker_accepted')
            second=peer.messages.get(timeout=3)
            self.assertEqual((first.topic,first.payload),(second.topic,second.payload))
            self.assertEqual(self.state(receipt)[1],1)
            stack.stop('publish')

    def test_all_gateway_workers_share_one_connection_budget(self):
        from contextlib import contextmanager
        from dataclasses import replace
        from backend.waveforms.config import CapacityLimits,load_waveform_config
        from backend.waveforms.consumer import DatabaseConnections
        from backend.waveforms.publisher import run_publish
        from tests.waveform_support import LIMITS
        from tests.waveform_stack import Stack
        limits=CapacityLimits(LIMITS.global_quota,{'A':LIMITS.gateways['GW'],'B':LIMITS.gateways['GW']})
        stop=threading.Event(); observed=[]; seen=set()
        original=DatabaseConnections.factory
        def factory(manager,role):
            self.assertEqual(role,'receipt_publisher')
            original_connect=original(manager,role)
            @contextmanager
            def connect():
                with original_connect() as connection:
                    observed.append(manager.active_count)
                    yield connection
            return connect
        def event(name,**details):
            if name=='idle':
                seen.add(details['gateway'])
                if seen=={'A','B'}: stop.set()
        with self.capacity_fixture(limits), Stack(self,limits=limits) as stack, patch.dict(os.environ,{'TEST_WAVEFORM_MQTT':stack.broker.password}):
            config=replace(load_waveform_config(stack.config,component='publish'),max_db_connections=1)
            timer=threading.Timer(10,stop.set); timer.start()
            try:
                with patch.object(DatabaseConnections,'factory',factory): run_publish(config,stop,on_event=event)
            finally: timer.cancel(); timer.join()
        self.assertEqual(seen,{'A','B'})
        self.assertEqual(max(observed),1)

    def test_close_during_connect_prevents_claim_and_uses_owner_cleanup(self):
        from concurrent.futures import ThreadPoolExecutor
        outbox=self.outbox(); receipt=self.receipt(); client=Client()
        entered=threading.Event(); release=threading.Event(); closed=[]
        original=client.connect
        def connect(*args,**kwargs):
            entered.set(); release.wait(5)
            return original(*args,**kwargs)
        client.connect=connect
        client._reset_sockets=lambda:closed.append(threading.current_thread().ident)
        publisher=self.publisher(outbox,client)
        with ThreadPoolExecutor(1) as pool:
            future=pool.submit(publisher.run_once)
            try:
                self.assertTrue(entered.wait(3)); publisher.close()
            finally: release.set()
            self.assertEqual(future.result(timeout=5),0)
        self.assertEqual(self.state(receipt)[2],None)
        self.assertEqual(len(closed),1)
        self.assertNotEqual(closed[0],threading.current_thread().ident)

    def test_nonretryable_outbox_failure_reaches_component_caller(self):
        from backend.waveforms.config import load_waveform_config
        from backend.waveforms.errors import WaveformError
        from backend.waveforms.outbox import WaveOutbox
        from backend.waveforms.publisher import run_publish
        from tests.waveform_stack import Stack
        stop=threading.Event()
        with Stack(self) as stack, patch.dict(os.environ,{'TEST_WAVEFORM_MQTT':stack.broker.password}):
            config=load_waveform_config(stack.config,component='publish')
            with patch.object(WaveOutbox,'claim',side_effect=WaveformError('waveform.invariant','synthetic failure')):
                with self.assertRaises(WaveformError) as caught: run_publish(config,stop)
            self.assertEqual(caught.exception.code,'waveform.invariant')

    def publisher(self,outbox,client):
        self.assertIsNotNone(importlib.util.find_spec('backend.waveforms.publisher'),'publisher is required')
        from backend.waveforms.publisher import WavePublisher
        return WavePublisher(outbox,runtime_config(self,'publish').gateways[0],client_factory=lambda _gateway:client,ack_timeout=.02)

    def test_callback_before_publish_return(self):
        outbox=self.outbox(); receipt=self.receipt(); client=Client()
        publisher=self.publisher(outbox,client)
        self.assertEqual(publisher.run_once(),1)
        self.assertEqual(self.state(receipt)[1],1)
        self.assertEqual(client.sent,[('oil/v2/GW/D/waveform-ack',b'{ "persisted":true }',1,False)])
        self.assertTrue(client.connect_options['clean_start'])
        self.assertEqual(client.connect_options['properties'].SessionExpiryInterval,0)

    def test_mid_reuse_after_reconnect(self):
        outbox=self.outbox(); receipt=self.receipt(); first=Client()
        publisher=self.publisher(outbox,first)
        saved=[]
        original=first.publish
        def publish(*args,**kwargs):
            saved.append(first.on_publish)
            return original(*args,**kwargs)
        first.publish=publish
        self.assertEqual(publisher.run_once(),1)
        with self.connect() as conn:
            conn.execute('UPDATE waveforms.deliveries SET requested_generation=2 WHERE receipt_id=%s',(receipt,))
        second=Client('lost',old=saved[0])
        publisher.client_factory=lambda _:second
        self.assertEqual(publisher.run_once(),0)
        self.assertEqual(self.state(receipt)[1],1)

    def test_puback_reason_semantics(self):
        for mode,expected in [('negative',0),('no_subscribers',1)]:
            with self.subTest(mode=mode):
                outbox=self.outbox(); receipt=self.receipt()
                self.assertEqual(self.publisher(outbox,Client(mode)).run_once(),expected)
                self.assertEqual(self.state(receipt)[1],expected)

    def test_failed_disposal_prevents_replacement(self):
        from backend.waveforms.errors import WaveformError
        outbox=self.outbox(); client=Client()
        publisher=self.publisher(outbox,client)
        client._reset_sockets=lambda: (_ for _ in ()).throw(OSError())
        with self.assertRaises(WaveformError) as caught: publisher.run_once()
        self.assertEqual(caught.exception.code,'waveform.shutdown_incomplete')
        replacement=Client()
        publisher.client_factory=lambda _:replacement
        publisher.run_once()
        self.assertFalse(hasattr(replacement,'connect_options'))

    def test_real_broker_readiness_and_committed_acceptance(self):
        from backend.waveforms.config import load_waveform_config
        from backend.waveforms.consumer import check_component,DatabaseConnections
        from backend.waveforms.outbox import WaveOutbox
        from backend.waveforms.publisher import WavePublisher
        from tests.waveform_stack import Stack
        receipt=self.receipt()
        with Stack(self) as stack, patch.dict(os.environ,{'TEST_WAVEFORM_MQTT':stack.broker.password}):
            config=load_waveform_config(stack.config,component='publish')
            connections=DatabaseConnections(config)
            check_component(config,connections)
            self.assertEqual(self.scalar('SELECT version FROM waveforms.state_head',role='receipt_publisher'),1)
            publisher=WavePublisher(WaveOutbox(connections.factory('receipt_publisher'),gateway_id='GW',limits=config.limits),config.gateways[0])
            self.assertEqual(publisher.run_once(),1)
            self.assertEqual(self.state(receipt)[1],1)
            self.assertEqual(connections.active_count,0)

    def test_cli_publish_check_and_run_stop(self):
        from tests.waveform_stack import Stack
        self.receipt()
        with Stack(self) as stack:
            process=stack.launch('publish',check=True)
            self.assertEqual(process.wait(timeout=10),0)
            stack.stop('publish')
            stack.launch('publish')
            stack.await_event('publish','broker_accepted')
            stack.stop('publish')
            self.assertEqual(stack.events('publish','stopped')[-1]['remaining_threads'],[])

    def test_unknown_success_commit_has_no_followup_failure_write(self):
        from contextlib import contextmanager
        import psycopg
        from backend.waveforms.errors import WaveformError
        outbox=self.outbox(); receipt=self.receipt(); original=outbox.finish
        outcomes=[]
        class CommitLost:
            def __init__(self,connection): self.connection=connection
            def __getattr__(self,name): return getattr(self.connection,name)
            def commit(self):
                self.connection.commit()
                raise psycopg.OperationalError('synthetic uncertain response')
        @contextmanager
        def connect():
            with self.connect('receipt_publisher') as connection: yield CommitLost(connection)
        def finish(lease,**kwargs):
            outcomes.append(kwargs['delivered'])
            with patch.object(outbox,'_connect',connect): return original(lease,**kwargs)
        with patch.object(outbox,'finish',side_effect=finish):
            with self.assertRaises(WaveformError): self.publisher(outbox,Client()).run_once()
        self.assertEqual(outcomes,[True])
        self.assertEqual(self.state(receipt)[1],1)
        self.assertIsNone(self.state(receipt)[4])

    def test_real_broker_acl_rejects_uplink_and_other_gateway(self):
        from backend.waveforms.config import load_waveform_config
        from backend.waveforms.publisher import WavePublisher
        from tests.waveform_stack import Stack
        from tests.ingest_broker import Peer
        outbox=self.outbox(); receipt=self.receipt(topic='oil/v2/GW/D/waveform')
        with Stack(self) as stack, patch.dict(os.environ,{'TEST_WAVEFORM_MQTT':stack.broker.password}):
            gateway=load_waveform_config(stack.config,component='publish').gateways[0]
            self.assertEqual(WavePublisher(outbox,gateway).run_once(),0)
            self.assertEqual(self.state(receipt)[1],0)
            self.assertEqual(self.state(receipt)[4],'mqtt.publish_rejected')
            peer=Peer(stack.broker,'waveform-GW-publish'); stack.broker.clients.append(peer)
            self.assertEqual(peer.publish('oil/v2/OTHER/D/waveform-ack',b'synthetic'),135)

    def test_lost_puback_and_broker_restart_replay_exact_persisted_receipt(self):
        from backend.waveforms.config import load_waveform_config
        from backend.waveforms.consumer import create_client
        from backend.waveforms.publisher import WavePublisher
        from tests.waveform_stack import Stack
        from tests.ingest_broker import Peer
        outbox=self.outbox(); receipt=self.receipt()
        with Stack(self) as stack, patch.dict(os.environ,{'TEST_WAVEFORM_MQTT':stack.broker.password}):
            gateway=load_waveform_config(stack.config,component='publish').gateways[0]
            peer=Peer(stack.broker,'waveform-GW-sender'); stack.broker.clients.append(peer)
            self.assertEqual(peer.subscribe('oil/v2/GW/D/waveform-ack'),[1])
            def losing(gateway):
                client=create_client(gateway); publish=client.publish
                def send(*args,**kwargs):
                    client.on_publish=lambda *_:None
                    return publish(*args,**kwargs)
                client.publish=send
                return client
            publisher=WavePublisher(outbox,gateway,client_factory=losing,ack_timeout=.3)
            self.assertEqual(publisher.run_once(),0)
            first=peer.messages.get(timeout=3)
            self.assertEqual(self.state(receipt)[1],0)
            peer.close(); stack.broker.clients.remove(peer)
            stack.broker.stop(); stack.broker.start()
            peer=Peer(stack.broker,'waveform-GW-sender'); stack.broker.clients.append(peer)
            peer.subscribe('oil/v2/GW/D/waveform-ack')
            self.now+=timedelta(seconds=2)
            publisher.client_factory=create_client
            self.assertEqual(publisher.run_once(),1)
            second=peer.messages.get(timeout=3)
            self.assertEqual((first.topic,first.payload),(second.topic,second.payload))
            self.assertEqual(second.payload,b'{ "persisted":true }')
            self.assertEqual(self.state(receipt)[1],1)

    def test_actual_socket_disposal_after_would_block_disconnect(self):
        from backend.waveforms.config import load_waveform_config
        from backend.waveforms.consumer import create_client
        from backend.waveforms.publisher import WavePublisher
        from tests.waveform_stack import Stack
        outbox=self.outbox(); self.receipt(); records=[]
        def factory(gateway):
            client=create_client(gateway); send=client._sock_send; disconnect=client.disconnect
            def blocked(data):
                if data[0]==0xe0: raise BlockingIOError()
                return send(data)
            def close():
                sockets=(client.socket(),client._sockpairR,client._sockpairW)
                result=disconnect()
                records.append((client,sockets,result,client.want_write()))
                return result
            client._sock_send=blocked; client.disconnect=close
            return client
        with Stack(self) as stack, patch.dict(os.environ,{'TEST_WAVEFORM_MQTT':stack.broker.password}):
            gateway=load_waveform_config(stack.config,component='publish').gateways[0]
            self.assertEqual(WavePublisher(outbox,gateway,client_factory=factory).run_once(),1)
        client,sockets,result,queued=records[0]
        self.assertEqual(result,0); self.assertTrue(queued)
        self.assertEqual([sock.fileno() for sock in sockets],[-1,-1,-1])
        self.assertIsNone(client.on_publish); self.assertIsNone(client.on_disconnect)
