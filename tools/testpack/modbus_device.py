"""Bounded loopback-only Modbus TCP software device. FC03/04, no writes."""
from collections import deque
from datetime import datetime,timezone
from hashlib import sha256
import math
import socket
import socketserver
import struct
import threading


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads=True
    block_on_close=False
    allow_reuse_address=False
    request_queue_size=16

    def process_request(self,request,client_address):
        if not self.slots.acquire(blocking=False):
            request.close();return
        try:super().process_request(request,client_address)
        except BaseException:
            self.slots.release();raise

    def process_request_thread(self,request,client_address):
        try:super().process_request_thread(request,client_address)
        finally:self.slots.release()


class _Handler(socketserver.BaseRequestHandler):
    def _read(self,n):
        data=b''
        while len(data)<n:
            block=self.request.recv(n-len(data))
            if not block:raise EOFError
            data+=block
        return data

    def handle(self):
        self.request.settimeout(2)
        try:
            for _ in range(256):
                header=self._read(7)
                transaction,protocol,length,unit=struct.unpack('>HHHB',header)
                if protocol!=0 or not 2<=length<=254:return
                pdu=self._read(length-1)
                result=self.server.owner._respond(unit,pdu)
                response=struct.pack('>HHHB',transaction,0,len(result)+1,unit)+result
                with self.server.owner.lock:
                    self.server.owner.exchanges.append({'at':datetime.now(timezone.utc).isoformat(),'unit_id':unit,
                        'function_code':pdu[0],'request_sha256':sha256(header+pdu).hexdigest(),
                        'response_sha256':sha256(response).hexdigest()})
                self.request.sendall(response)
        except (EOFError,OSError,struct.error):
            return


class ReadOnlyModbusDevice:
    def __init__(self,maps,*,host,port):
        if host!='127.0.0.1' or type(port) is not int or not 0<=port<=65535 or not maps or set(maps)-{1,2}:
            raise ValueError('bounded loopback unit 1/2 device required')
        self.maps=maps
        for unit,points in maps.items():
            addresses=[]
            for point in points:
                if point.unit_id!=unit or point.function_code not in (3,4) or point.data_type!='float32' or point.writable:
                    raise ValueError('read-only float32 maps required')
                if point.byte_order!='big' or point.word_order!='big' or point.scale!=1 or point.offset!=0:
                    raise ValueError('native big-endian engineering floats required')
                addresses.extend((point.address,point.address+1))
            if len(addresses)!=len(set(addresses)) or len(points)!=len({p.point_code for p in points}):
                raise ValueError('overlapping map')
        self.host,self.port=host,port
        self.banks={};self.lock=threading.Lock();self.exchanges=deque(maxlen=4096)
        self.server=None;self.thread=None

    def start(self):
        if self.server is not None:raise ValueError('software device already started')
        server=_Server((self.host,self.port),_Handler)
        server.owner=self;server.slots=threading.BoundedSemaphore(16)
        self.port=server.server_address[1];self.server=server
        self.thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.05},daemon=True)
        self.thread.start()
        return self

    def set_frame(self,unit_id,values):
        if unit_id not in self.maps or set(values)!={p.point_code for p in self.maps[unit_id]}:
            raise ValueError('complete registered frame required')
        bank={}
        for point in self.maps[unit_id]:
            value=values[point.point_code]
            if type(value) not in (int,float) or not math.isfinite(value):raise ValueError('finite numeric frame required')
            try:encoded=struct.pack('>f',value)
            except (OverflowError,struct.error) as exc:raise ValueError('float32 frame overflow') from exc
            bank[point.address]=encoded[:2];bank[point.address+1]=encoded[2:]
        with self.lock:self.banks[unit_id]=bank

    def _respond(self,unit,pdu):
        function=pdu[0]
        def error(code):return bytes((function|128,code))
        if function not in (3,4):return error(1)
        if len(pdu)!=5:return error(3)
        start,count=struct.unpack('>HH',pdu[1:])
        if not 1<=count<=125:return error(3)
        with self.lock:
            bank=self.banks.get(unit)
            if bank is None or any(i not in bank for i in range(start,start+count)):return error(2)
            return bytes((function,count*2))+b''.join(bank[i] for i in range(start,start+count))

    def stop(self):
        if self.server is not None:
            self.server.shutdown();self.server.server_close();self.thread.join(3)
            self.server=None;self.thread=None
