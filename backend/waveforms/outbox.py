"""Gateway-bound durable application receipt leases; no MQTT work in transactions."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
import time
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL
import psycopg
from .capacity import prepare_transaction, lock_gateway, row
from .config import CapacityLimits
from .errors import WaveformError


def _database_failure(error):
    if isinstance(error,psycopg.errors.InsufficientPrivilege):
        code='waveform.privilege_unsafe'
    elif isinstance(error,(psycopg.IntegrityError,psycopg.DataError,psycopg.ProgrammingError)):
        code='waveform.invariant'
    else:
        code='waveform.unavailable'
    return WaveformError(code,'Receipt transaction did not return a durable result')


@dataclass(frozen=True)
class DeliveryLease:
    receipt_id: UUID
    generation: int
    lease_token: UUID
    gateway_id: str
    topic: str
    payload: bytes
    lease_until: datetime
    _deadline: float


class WaveOutbox:
    def __init__(self, connect, *, gateway_id, limits, clock=None):
        if (not callable(connect) or type(limits) is not CapacityLimits
                or gateway_id not in limits.gateways or clock is not None and not callable(clock)):
            raise ValueError('Explicit gateway, capacities and connection required')
        self._connect,self._gateway_id,self._limits,self._clock=connect,gateway_id,limits,clock

    @property
    def gateway_id(self):
        return self._gateway_id

    def _now(self,cursor):
        if self._clock is None:
            cursor.execute('SELECT clock_timestamp()')
            return cursor.fetchone()[0].astimezone(timezone.utc)
        now=self._clock()
        if not isinstance(now,datetime) or now.tzinfo is None or now.utcoffset()!=timedelta(0):
            raise ValueError('Clock must return aware UTC')
        return now

    def _health(self,cursor,now,*,error=None,success=False,event=False):
        cursor.execute("SELECT error_code FROM waveforms.worker_health WHERE component='receipt_publisher' AND gateway_id=%s FOR UPDATE",(self.gateway_id,))
        previous=cursor.fetchone()
        changed=(previous is not None and previous[0]!=error) or error is not None and previous is None
        cursor.execute("""INSERT INTO waveforms.worker_health(worker_id,component,gateway_id,last_seen,last_success,last_failure,error_code)
            VALUES(%s,'receipt_publisher',%s,%s,%s,%s,%s)
            ON CONFLICT(component,gateway_id) DO UPDATE SET
            last_seen=greatest(waveforms.worker_health.last_seen,excluded.last_seen),
            last_success=coalesce(excluded.last_success,waveforms.worker_health.last_success),
            last_failure=coalesce(excluded.last_failure,waveforms.worker_health.last_failure),error_code=excluded.error_code""",
            (uuid5(NAMESPACE_URL,'waveforms/receipt_publisher/'+self.gateway_id),self.gateway_id,now,
             now if success else None,now if error and (changed or event) else None,error))
        return changed

    def _head(self,cursor):
        cursor.execute('UPDATE waveforms.state_head SET version=version+1 WHERE singleton=1')

    def _locked(self,cursor,*,limit=0,receipt=None):
        capacity=lock_gateway(cursor,self.gateway_id,self._limits)
        now=self._now(cursor)
        cursor.execute('SELECT receipt_id FROM waveforms.deliveries WHERE intake_gateway_id=%s AND lease_token IS NOT NULL ORDER BY receipt_id LIMIT 65',(self.gateway_id,))
        active=[r[0] for r in cursor.fetchall()]
        if len(active)>64:
            raise WaveformError('waveform.invariant','Gateway lease bound exceeded')
        ids=set(active)
        if receipt is not None: ids.add(receipt)
        if limit:
            cursor.execute('''SELECT receipt_id FROM waveforms.deliveries WHERE intake_gateway_id=%s
                AND requested_generation>sent_generation AND next_attempt_at<=%s
                AND (lease_token IS NULL OR lease_until<=%s) ORDER BY receipt_id LIMIT %s''',(self.gateway_id,now,now,limit))
            ids.update(r[0] for r in cursor.fetchall())
        cursor.execute('SELECT * FROM waveforms.deliveries WHERE intake_gateway_id=%s AND receipt_id=ANY(%s) ORDER BY receipt_id FOR UPDATE',(self.gateway_id,list(ids)))
        deliveries=[]
        while (item:=row(cursor)) is not None: deliveries.append(item)
        # The gateway lock precedes this read, but an outside row-lock holder
        # can still consume the lease lifetime while ordered delivery locks wait.
        now=self._now(cursor)
        high=capacity['last_clock_seen']
        if high is not None and now<high:
            for item in deliveries:
                if item['lease_token'] is not None:
                    cursor.execute("UPDATE waveforms.deliveries SET lease_token=NULL,lease_generation=NULL,lease_until=NULL,last_error_code='clock_rollback',version=version+1 WHERE receipt_id=%s",(item['receipt_id'],))
            changed=self._health(cursor,now,error='clock_rollback')
            if active or changed: self._head(cursor)
            return now,deliveries,False,len(active)
        cursor.execute('UPDATE waveforms.capacity SET last_clock_seen=%s WHERE bucket=%s',(now,self.gateway_id))
        return now,deliveries,True,len(active)

    def claim(self,worker_id:UUID,*,limit:int)->tuple[DeliveryLease,...]:
        if type(worker_id) is not UUID or type(limit) is not int or not 1<=limit<=64:
            raise ValueError('UUID worker and limit 1..64 required')
        leases=[]
        try:
            with self._connect() as connection:
                cursor=connection.cursor(); prepare_transaction(cursor)
                now,items,ready,count=self._locked(cursor,limit=limit)
                # Start local lifetime only after database locks are held.
                deadline=time.monotonic()+30
                if ready:
                    for item in items:
                        if len(leases)>=limit: break
                        if (item['requested_generation']<=item['sent_generation'] or item['next_attempt_at']>now
                                or item['lease_token'] is not None and item['lease_until']>now): continue
                        if item['lease_token'] is None:
                            if count>=64: continue
                            count+=1
                        token=uuid4(); until=now+timedelta(seconds=30)
                        cursor.execute('UPDATE waveforms.deliveries SET lease_token=%s,lease_generation=requested_generation,lease_until=%s,version=version+1 WHERE receipt_id=%s',(token,until,item['receipt_id']))
                        cursor.execute('SELECT topic,payload FROM waveforms.receipts WHERE receipt_id=%s AND intake_gateway_id=%s',(item['receipt_id'],self.gateway_id))
                        topic,payload=cursor.fetchone()
                        leases.append(DeliveryLease(item['receipt_id'],item['requested_generation'],token,self.gateway_id,topic,bytes(payload),until,deadline))
                    changed=self._health(cursor,now)
                    if leases or changed: self._head(cursor)
                prepare_transaction(cursor)
                connection.commit()
            return tuple(leases)
        except psycopg.Error as error:
            raise _database_failure(error) from None

    def finish(self,lease,*,delivered:bool,error_code:str|None)->bool:
        if type(lease) is not DeliveryLease or type(delivered) is not bool:
            raise ValueError('Delivery lease and boolean result required')
        if not delivered and error_code is None:
            error_code='delivery.failed'
        if delivered and error_code is not None or not delivered and (not isinstance(error_code,str) or not re.fullmatch('[a-z][a-z0-9_.]{0,95}',error_code)):
            raise ValueError('Controlled delivery result required')
        if lease.gateway_id!=self.gateway_id: return False
        finished=False
        try:
            with self._connect() as connection:
                cursor=connection.cursor(); prepare_transaction(cursor)
                now,items,ready,_=self._locked(cursor,receipt=lease.receipt_id)
                item=next((item for item in items if item['receipt_id']==lease.receipt_id),None)
                if (ready and item and time.monotonic()<lease._deadline and item['lease_until'] is not None
                        and item['lease_until']>now and item['lease_token']==lease.lease_token
                        and item['lease_generation']==lease.generation):
                    attempts=min(2147483647,item['attempts']+1)
                    delay=min(300,2**min(attempts,9))
                    cursor.execute('''UPDATE waveforms.deliveries SET sent_generation=%s,lease_token=NULL,
                        lease_generation=NULL,lease_until=NULL,attempts=%s,last_error_code=%s,
                        broker_acked_at=%s,next_attempt_at=%s,version=version+1 WHERE receipt_id=%s''',
                        (lease.generation if delivered else item['sent_generation'],attempts,error_code,
                         now if delivered else item['broker_acked_at'],now if delivered else now+timedelta(seconds=delay),lease.receipt_id))
                    self._health(cursor,now,error=error_code,success=delivered,event=True)
                    self._head(cursor); finished=True
                prepare_transaction(cursor)
                connection.commit()
            return finished
        except psycopg.Error as error:
            # The commit may have succeeded; caller must never follow with a failure write.
            raise _database_failure(error) from None

    def record_error(self,error_code):
        """Bounded connection-failure health, preserving a rollback freeze."""
        if not isinstance(error_code,str) or not re.fullmatch('[a-z][a-z0-9_.]{0,95}',error_code):
            raise ValueError('Controlled error code required')
        try:
            with self._connect() as connection:
                cursor=connection.cursor(); prepare_transaction(cursor)
                now,_,ready,_=self._locked(cursor)
                if ready and self._health(cursor,now,error=error_code): self._head(cursor)
                prepare_transaction(cursor)
                connection.commit()
        except psycopg.Error as error:
            raise _database_failure(error) from None
