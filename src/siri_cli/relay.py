"""Transport-neutral offline relay. No Messages access or live Siri invocation.

Providers must cooperate with cancellation/deadlines. A blocked provider retains
the single Siri lock; cancelling a request never permits overlapping UI work.
"""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid
import struct
import zlib
import fcntl


class RelayFailure(Exception):
    pass


@dataclass(frozen=True)
class Incoming:
    message_id: str
    conversation_id: str
    query: str
    timeout_s: float = 120
    transport: str = 'fake'
    account_id: str = ''


@dataclass(frozen=True)
class Ticket:
    request_id: str
    incoming: Incoming
    directory: Path
    deadline: float
    cancel: threading.Event

    def check(self):
        if self.cancel.is_set():raise RelayFailure('cancelled')
        if time.monotonic()>=self.deadline:raise RelayFailure('deadline')


@dataclass(frozen=True)
class Preview:
    ticket: Ticket
    text: str | None = None
    image: Path | None = None
    scoped: bool = False
    fresh_turn: bool = False
    identity_verified: bool = False
    readable: bool = False
    source: str = 'siri'


@dataclass(frozen=True)
class Reply:
    request_id: str
    message_id: str
    conversation_id: str
    text: str | None
    image: bytes | None
    status: str = 'preview'
    completion: str = 'unverified'
    account_id: str = ''


def validate_png(data):
    """Strict lossless RGB/RGBA PNG subset; validity is not readability proof."""
    if not data.startswith(b'\x89PNG\r\n\x1a\n'):raise RelayFailure('attachment_invalid')
    offset=8;compressed=bytearray();header=None;ended=False
    while offset+12<=len(data):
        size=struct.unpack('>I',data[offset:offset+4])[0]
        kind=data[offset+4:offset+8];body=data[offset+8:offset+8+size]
        if offset+12+size>len(data):raise RelayFailure('attachment_invalid')
        crc=struct.unpack('>I',data[offset+8+size:offset+12+size])[0]
        if zlib.crc32(kind+body)&0xffffffff!=crc:raise RelayFailure('attachment_invalid')
        if header is None:
            if kind!=b'IHDR' or size!=13:raise RelayFailure('attachment_invalid')
            header=struct.unpack('>IIBBBBB',body)
        elif kind==b'IHDR':raise RelayFailure('attachment_invalid')
        if kind==b'IDAT':compressed.extend(body)
        offset+=12+size
        if kind==b'IEND':ended=True;break
    if not ended or offset!=len(data) or header is None:raise RelayFailure('attachment_invalid')
    w,h,depth,color,comp,filt,interlace=header
    if not (w>0 and h>0 and w*h<=20_000_000 and depth==8 and color in (2,6) and (comp,filt,interlace)==(0,0,0)):
        raise RelayFailure('attachment_unsupported')
    row=w*(3 if color==2 else 4)+1;expected=row*h
    try:
        decoder=zlib.decompressobj();pixels=decoder.decompress(compressed,expected+1)
    except zlib.error:raise RelayFailure('attachment_invalid')
    if len(pixels)!=expected or not decoder.eof or decoder.unused_data or any(pixels[i]>4 for i in range(0,len(pixels),row)):
        raise RelayFailure('attachment_invalid')


class Relay:
    def __init__(self,journal,workspace,provider,sender):
        self.workspace=Path(workspace);self.workspace.mkdir(parents=True,exist_ok=True)
        self.provider=provider;self.sender=sender
        self.ui_lock=threading.Lock();self.db_lock=threading.RLock()
        self.db=sqlite3.connect(journal,check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS requests (key TEXT PRIMARY KEY, fingerprint TEXT, request_id TEXT, status TEXT, error TEXT)')
        self.db.commit()

    def _save(self,key,request_id,status,error=None):
        with self.db_lock:
            self.db.execute('UPDATE requests SET status=?,error=? WHERE key=?',(status,error,key));self.db.commit()
        return {'request_id':request_id,'status':status,'error':error}

    def reconcile_delivery(self,incoming):
        """Receipt-only recovery: never reruns Siri and never issues a send."""
        key=json.dumps([incoming.transport,incoming.message_id])
        fingerprint=hashlib.sha256(json.dumps([incoming.account_id,incoming.conversation_id,incoming.query]).encode()).hexdigest()
        with self.db_lock:
            row=self.db.execute('SELECT fingerprint,request_id,status,error FROM requests WHERE key=?',(key,)).fetchone()
        if row is None or row[0]!=fingerprint:raise RelayFailure('reconcile_identity_mismatch')
        if row[2] not in ('sending','delivery_uncertain'):
            return {'request_id':row[1],'status':row[2],'error':row[3]}
        try:status=self.sender.reconcile(row[1])
        except Exception:status='uncertain'
        if status=='submitted':return self._save(key,row[1],'sent_reconciled')
        return {'request_id':row[1],'status':row[2],'error':'reconciliation_unresolved'}

    def handle(self,incoming,cancel=None):
        started=time.monotonic()
        if not all(isinstance(v,str) and v for v in (incoming.message_id,incoming.conversation_id,incoming.query,incoming.transport)) or not 0<incoming.timeout_s<=120:
            raise ValueError('invalid incoming envelope')
        key=json.dumps([incoming.transport,incoming.message_id])
        fingerprint=hashlib.sha256(json.dumps([incoming.account_id,incoming.conversation_id,incoming.query]).encode()).hexdigest()
        request_id=str(uuid.uuid4())
        with self.db_lock:
            inserted=self.db.execute('INSERT OR IGNORE INTO requests VALUES (?,?,?,?,?)',(key,fingerprint,request_id,'pending',None)).rowcount
            self.db.commit()
            if not inserted:
                row=self.db.execute('SELECT fingerprint,request_id,status,error FROM requests WHERE key=?',(key,)).fetchone()
                return {'request_id':row[1],'status':row[2] if row[0]==fingerprint else 'rejected',
                        'error':row[3] if row[0]==fingerprint else 'message_id_conflict'}
        directory=self.workspace/request_id;directory.mkdir()
        ticket=Ticket(request_id,incoming,directory,started+incoming.timeout_s,cancel or threading.Event())
        acquired=False;lease=None
        try:
            while not acquired:
                ticket.check();acquired=self.ui_lock.acquire(timeout=min(.02,max(0,ticket.deadline-time.monotonic())))
            ticket.check()
            lease=open('/tmp/siri-cli-current-turn.lock','a+')
            while True:
                ticket.check()
                try:fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:time.sleep(min(.02,max(0,ticket.deadline-time.monotonic())))
            self._save(key,request_id,'processing')
            # Exactly one provider call. Uncertain submissions are never retried.
            candidate=self.provider(ticket)
            ticket.check()
            if not isinstance(candidate,Preview) or candidate.ticket is not ticket or candidate.source!='siri':
                raise RelayFailure('wrong_request')
            if not (candidate.scoped and candidate.fresh_turn and candidate.identity_verified):raise RelayFailure('unverified_turn')
            if candidate.text is not None and (not isinstance(candidate.text,str) or not candidate.text.strip()):raise RelayFailure('invalid_text')
            image=None
            if candidate.image is not None:
                path=Path(candidate.image).resolve()
                if not path.is_relative_to(directory.resolve()) or not candidate.readable:raise RelayFailure('unverified_attachment')
                try:image=path.read_bytes()
                except OSError:raise RelayFailure('attachment_missing')
                if len(image)>20_000_000:raise RelayFailure('attachment_too_large')
                validate_png(image)
            if candidate.text is None and image is None:raise RelayFailure('empty_result')
            ticket.check()
            reply=Reply(request_id,incoming.message_id,incoming.conversation_id,candidate.text,image,account_id=incoming.account_id)
            self._save(key,request_id,'sending')
            try:self.sender(reply)
            except Exception:return self._save(key,request_id,'delivery_uncertain','sender_failure')
            # Once send starts it cannot be recalled. Never silently retry it.
            if ticket.cancel.is_set() or time.monotonic()>=ticket.deadline:
                return self._save(key,request_id,'sent_after_stop','delivery_started_before_stop')
            return self._save(key,request_id,'sent')
        except RelayFailure as exc:return self._save(key,request_id,'failed',str(exc))
        except Exception:return self._save(key,request_id,'failed','provider_failure')
        finally:
            if lease is not None:lease.close()
            if acquired:self.ui_lock.release()


def live_provider_disabled(ticket):
    raise RelayFailure('live_request_bound_preview_adapter_not_connected')
