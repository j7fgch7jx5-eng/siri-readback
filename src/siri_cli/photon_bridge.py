"""Authenticated durable Photon-to-Mac jobs; no listener or SDK initialization.

The owner supplies verified project/line/space/sender bindings and a separate
32-byte IPC key. Spectrum project secrets are never used as job signing keys.
"""
import fcntl
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import time
from .relay import Incoming,Relay,RelayFailure,validate_png


def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
def sign(event,key):return hmac.new(key,canonical(event),hashlib.sha256).hexdigest()


class PhotonBridge:
    def __init__(self,root,project,line,routes,key,provider,service):
        if len(key)<32 or not project or not line or not routes:raise RelayFailure('binding_or_ipc_key_missing')
        # routes map exact original service space -> exact authorized sender.
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.project=project;self.line=line;self.routes=dict(routes);self.key=key;self.service=service
        self.db=sqlite3.connect(self.root/'jobs.sqlite')
        self.db.executescript('CREATE TABLE IF NOT EXISTS scope(value TEXT); CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,event TEXT,status TEXT); CREATE TABLE IF NOT EXISTS sends(run TEXT PRIMARY KEY,job TEXT,hash TEXT,status TEXT,receipt TEXT);')
        scope=canonical([project,line,routes,hashlib.sha256(key).hexdigest()]).decode()
        old=self.db.execute('SELECT value FROM scope').fetchone()
        if old and old[0]!=scope:raise RelayFailure('bridge_scope_changed')
        if not old:self.db.execute('INSERT INTO scope VALUES (?)',(scope,));self.db.commit()
        self.relay=Relay(self.root/'relay.sqlite',self.root/'artifacts',provider,self)

    def accept(self,event,signature):
        if not hmac.compare_digest(sign(event,self.key),signature):raise RelayFailure('job_authentication_failed')
        required={'project','line','space','sender','message','query','created','expires','direction','platform'}
        if set(event)!=required:raise RelayFailure('job_schema_invalid')
        if (event['project'],event['line'],event['direction'],event['platform'])!=(self.project,self.line,'inbound','imessage'):
            raise RelayFailure('forged_service_route')
        if self.routes.get(event['space'])!=event['sender']:raise RelayFailure('unauthorized_service_sender_or_space')
        if not all(isinstance(event[k],str) and event[k] for k in ('message','space','sender','query')):raise RelayFailure('job_identity_missing')
        job=hashlib.sha256(canonical([event['project'],event['message']])).hexdigest()
        old=self.db.execute('SELECT event FROM jobs WHERE id=?',(job,)).fetchone()
        if old:
            if old[0]!=canonical(event).decode():raise RelayFailure('duplicate_job_identity_conflict')
            return job  # Authenticated duplicate may arrive after expiry; no execution.
        now=time.time()
        if not (event['created']<=now<event['expires']<=event['created']+120) or event['created']<now-120:
            raise RelayFailure('stale_job')
        with self.db:
            old=self.db.execute('SELECT event FROM jobs WHERE id=?',(job,)).fetchone()
            if old and old[0]!=canonical(event).decode():raise RelayFailure('duplicate_job_identity_conflict')
            self.db.execute('INSERT OR IGNORE INTO jobs VALUES (?,?,?)',(job,canonical(event).decode(),'pending'))
        return job

    def _incoming(self,event):
        return Incoming(event['message'],event['space'],event['query'],max(.001,min(120,event['expires']-time.time())),
                        'photon:'+self.project,self.project)

    def _job_for_reply(self,reply):
        rows=self.db.execute('SELECT id,event FROM jobs').fetchall()
        matches=[(job,json.loads(raw)) for job,raw in rows if json.loads(raw)['message']==reply.message_id]
        if len(matches)!=1:raise RelayFailure('reply_job_ambiguous')
        job,event=matches[0]
        if (reply.account_id,reply.conversation_id)!=(self.project,event['space']):raise RelayFailure('reply_route_mismatch')
        return job,event

    def _receipt(self,receipt,event,digest):
        # Service adapter snapshots an actual SDK outbound Message. Application
        # job/hash correlation is attested by the authenticated service process;
        # it is not a claim that Spectrum accepts a client idempotency key.
        if not isinstance(receipt,dict):raise RelayFailure('service_receipt_unverified')
        expected={'project':self.project,'line':self.line,'space':event['space'],'sender':event['sender'],
                  'request_message':event['message'],('payload_sha256' if 'payload_sha256' in receipt else 'png_sha256'):digest,'direction':'outbound','platform':'imessage'}
        if not isinstance(receipt,dict) or receipt.get('error') or any(receipt.get(k)!=v for k,v in expected.items()) or not isinstance(receipt.get('service_message'),str) or not receipt['service_message']:
            raise RelayFailure('service_receipt_unverified')
        return receipt

    def __call__(self,reply):
        job,event=self._job_for_reply(reply)
        if time.time()>=event['expires']:raise RelayFailure('stale_preview')
        if reply.image is None:raise RelayFailure('request_bound_png_required')
        validate_png(reply.image);digest=hashlib.sha256(reply.image).hexdigest()
        if reply.text is not None:digest=hashlib.sha256(canonical({'png':digest,'text':reply.text})).hexdigest()
        with self.db:
            if self.db.execute("SELECT 1 FROM sends WHERE status!='accepted'").fetchone():raise RelayFailure('service_queue_uncertain')
            old=self.db.execute('SELECT job,hash,status FROM sends WHERE run=?',(reply.request_id,)).fetchone()
            if old:
                if old!=(job,digest,'accepted'):raise RelayFailure('send_identity_conflict')
                return
            self.db.execute('INSERT INTO sends VALUES (?,?,?,?,NULL)',(reply.request_id,job,digest,'uncertain'))
        # Persist before any external call. Never call this again after timeout.
        native_receipt=(self.service.send_png(event,reply.request_id,reply.image,reply.text) if reply.text is not None else self.service.send_png(event,reply.request_id,reply.image))
        receipt=self._receipt(native_receipt,event,digest)
        with self.db:self.db.execute("UPDATE sends SET status='accepted',receipt=? WHERE run=?",(canonical(receipt).decode(),reply.request_id))

    def reconcile(self,run):
        row=self.db.execute('SELECT job,hash,status,receipt FROM sends WHERE run=?',(run,)).fetchone()
        if not row:return 'uncertain'
        if row[2]=='accepted':return 'submitted'
        event=json.loads(self.db.execute('SELECT event FROM jobs WHERE id=?',(row[0],)).fetchone()[0])
        receipt=self.service.receipt_only(event,run,row[1])
        if receipt is None:return 'uncertain'
        receipt=self._receipt(receipt,event,row[1])
        with self.db:self.db.execute("UPDATE sends SET status='accepted',receipt=? WHERE run=?",(canonical(receipt).decode(),run))
        return 'submitted'

    def work_once(self):
        with open(self.root/'worker.lock','a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise RelayFailure('mac_worker_busy')
            # All interrupted jobs are inspected before any later work.
            for job,raw in self.db.execute("SELECT id,event FROM jobs WHERE status IN ('active','sending','delivery_uncertain') ORDER BY rowid").fetchall():
                result=self.relay.reconcile_delivery(self._incoming(json.loads(raw)))
                if result['status'] in ('pending','processing','sending','delivery_uncertain'):raise RelayFailure('service_queue_reconciliation_required')
                with self.db:self.db.execute('UPDATE jobs SET status=? WHERE id=?',(result['status'],job))
            if self.db.execute("SELECT 1 FROM sends WHERE status!='accepted'").fetchone():raise RelayFailure('service_queue_reconciliation_required')
            for job,raw in self.db.execute("SELECT id,event FROM jobs WHERE status='pending' ORDER BY rowid").fetchall():
                event=json.loads(raw)
                if time.time()>=event['expires']:
                    with self.db:self.db.execute("UPDATE jobs SET status='expired' WHERE id=?",(job,))
                    continue
                with self.db:self.db.execute("UPDATE jobs SET status='active' WHERE id=?",(job,))
                result=self.relay.handle(self._incoming(event))
                with self.db:self.db.execute('UPDATE jobs SET status=? WHERE id=?',(result['status'],job))
                return result
        return None


class MailboxService:
    """Owner-only local durable IPC; HMAC authenticates requests and receipts.

    Local colocated receiver only. This is not a remote-network transport.
    Unknown native send IDs cannot be discovered from Mac Messages records.
    """
    def __init__(self,root,key):
        self.root=Path(root);self.key=key
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        for name in ('jobs','outgoing','receipts'):(self.root/name).mkdir(parents=True,exist_ok=True,mode=0o700)

    def _name(self,run):return hashlib.sha256(run.encode()).hexdigest()+'.json'

    def send_png(self,event,run,png,text=None):
        import base64,os
        payload={'event':event,'run':run,'png':base64.b64encode(png).decode(),'png_sha256':hashlib.sha256(png).hexdigest()}
        if text is not None:payload['text']=text
        raw=canonical({'payload':payload,'signature':sign(payload,self.key)})
        # Durable publication by hard-link; no partial file or overwrite.
        import tempfile
        fd,tmp=tempfile.mkstemp(dir=self.root/'outgoing')
        try:
            with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
            try:os.link(tmp,self.root/'outgoing'/self._name(run))
            except FileExistsError:raise RelayFailure('existing_service_outgoing_requires_reconciliation')
            directory=os.open(self.root/'outgoing',os.O_RDONLY)
            try:os.fsync(directory)
            finally:os.close(directory)
        finally:os.unlink(tmp)
        raise RelayFailure('awaiting_service_receipt')

    def receipt_only(self,event,run,digest):
        path=self.root/'receipts'/self._name(run)
        if not path.exists():return None
        envelope=json.loads(path.read_bytes());payload=envelope['payload']
        if not hmac.compare_digest(sign(payload,self.key),envelope['signature']):raise RelayFailure('forged_service_receipt')
        if payload.get('run')!=run:raise RelayFailure('wrong_service_run')
        return payload['receipt']

    def ingest(self,bridge):
        for path in sorted((self.root/'jobs').glob('*.json')):
            envelope=json.loads(path.read_bytes())
            bridge.accept(envelope['event'],envelope['signature'])
