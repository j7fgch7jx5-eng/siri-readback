"""Scoped Messages adapter against an explicitly supplied synthetic SQLite DB.

Live body reads require an explicit opt-in; defaults remain fixture only.
GUIDs are opaque keys: mapping is checked via joined rows, never GUID syntax.
"""
from dataclasses import dataclass
import hashlib
import json
import sqlite3
import threading
import fcntl
from pathlib import Path
from .relay import Incoming,RelayFailure


@dataclass(frozen=True)
class Route:
    account_id: str
    chat_guid: str
    chat_account_id: str
    account_login: str
    message_account_guid: str
    participants: frozenset
    self_handles: frozenset
    service: str = 'iMessage'

    def validate(self):
        if not all((self.account_id,self.chat_guid,self.chat_account_id,self.account_login,self.message_account_guid,self.participants,self.self_handles)):
            raise RelayFailure('missing_account_or_conversation_identity')
        if self.service!='iMessage' or self.participants & self.self_handles:
            raise RelayFailure('invalid_route_identity')


class MessagesInbox:
    def __init__(self,source,state_path,route,*,live_enabled=False):
        route.validate();self.source=source;self.route=route
        self.dispatch_lock_path=Path(str(state_path)+'.dispatch.lock')
        # A fixture marker prevents accidental real body reads in this milestone.
        if not live_enabled and source.execute("SELECT name FROM sqlite_master WHERE name='relay_fixture'").fetchone() is None:
            raise RelayFailure('live_message_body_reads_disabled')
        self.state=sqlite3.connect(state_path)
        self.state.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT)')
        self.state.execute('CREATE TABLE IF NOT EXISTS inbox (guid TEXT PRIMARY KEY, fingerprint TEXT, envelope TEXT, status TEXT)')
        scope=json.dumps([route.account_id,route.chat_guid,route.chat_account_id,route.account_login,route.message_account_guid,sorted(route.participants),sorted(route.self_handles),route.service])
        old=self.state.execute("SELECT value FROM meta WHERE key='scope'").fetchone()
        if old and old[0]!=scope:raise RelayFailure('checkpoint_scope_changed')
        if not old:
            self._mapping()
            cutoff=source.execute('SELECT COALESCE(MAX(ROWID),0) FROM message').fetchone()[0]
            self.state.executemany('INSERT INTO meta VALUES (?,?)',[('scope',scope),('cutoff',str(cutoff)),('checkpoint',str(cutoff))])
            anchor=source.execute('SELECT guid FROM message WHERE ROWID=?',(cutoff,)).fetchone()
            self.state.execute('INSERT INTO meta VALUES (?,?)',('anchor',json.dumps(anchor[0] if anchor else None)))
            self.state.commit()

    def _mapping(self):
        r=self.route
        chats=self.source.execute('SELECT ROWID,account_id,account_login,service_name FROM chat WHERE guid=?',(r.chat_guid,)).fetchall()
        if len(chats)!=1 or chats[0][1:]!=(r.chat_account_id,r.account_login,r.service):raise RelayFailure('chat_mapping_unverified')
        chat_id=chats[0][0]
        handles=frozenset(row[0] for row in self.source.execute('SELECT h.id FROM handle h JOIN chat_handle_join j ON j.handle_id=h.ROWID WHERE j.chat_id=?',(chat_id,)))
        if handles!=r.participants:raise RelayFailure('participants_changed')
        return chat_id

    def poll_once(self):
        chat_id=self._mapping();r=self.route
        cutoff=int(self.state.execute("SELECT value FROM meta WHERE key='cutoff'").fetchone()[0])
        checkpoint=int(self.state.execute("SELECT value FROM meta WHERE key='checkpoint'").fetchone()[0])
        maximum=self.source.execute('SELECT COALESCE(MAX(ROWID),0) FROM message').fetchone()[0]
        anchor=self.source.execute('SELECT guid FROM message WHERE ROWID=?',(cutoff,)).fetchone()
        expected=json.loads(self.state.execute("SELECT value FROM meta WHERE key='anchor'").fetchone()[0])
        if maximum<checkpoint or (anchor[0] if anchor else None)!=expected:
            raise RelayFailure('source_reset_or_cutoff_identity_changed')
        # Revisit only post-startup scoped rows, so edits to known rows are seen.
        rows=self.source.execute('SELECT DISTINCT m.ROWID,m.guid,m.text,m.is_from_me,m.is_system_message,m.date_edited,m.date_retracted,m.account_guid,m.service,h.id FROM message m JOIN chat_message_join j ON j.message_id=m.ROWID LEFT JOIN handle h ON h.ROWID=m.handle_id WHERE j.chat_id=? AND m.ROWID>? ORDER BY m.ROWID',(chat_id,cutoff)).fetchall()
        if len(rows)>1000:raise RelayFailure('scoped_batch_too_large')
        with self.state:
            for rowid,guid,text,outbound,system,edited,retracted,account,service,sender in rows:
                if not isinstance(guid,str) or not guid:raise RelayFailure('missing_message_guid')
                associations={x[0] for x in self.source.execute('SELECT DISTINCT chat_id FROM chat_message_join WHERE message_id IN (SELECT ROWID FROM message WHERE guid=?)',(guid,))}
                if associations!={chat_id}:raise RelayFailure('message_guid_chat_ambiguous')
                fingerprint=hashlib.sha256(json.dumps([text,outbound,system,edited,retracted,account,service,sender]).encode()).hexdigest()
                existing=self.state.execute('SELECT fingerprint,status FROM inbox WHERE guid=?',(guid,)).fetchone()
                if existing:
                    if existing[0]!=fingerprint:
                        self.state.execute("UPDATE inbox SET status='edited_blocked' WHERE guid=?",(guid,))
                    continue
                status='pending'
                if outbound or system or sender in r.self_handles:status='excluded'
                elif edited or retracted:status='edited_blocked'
                elif account!=r.message_account_guid or service!=r.service or sender not in r.participants:status='identity_blocked'
                elif not isinstance(text,str) or not text.strip():status='unsupported_body'
                envelope=json.dumps({'message_id':guid,'conversation_id':r.chat_guid,'query':text if status=='pending' else '',
                    'account_id':r.account_id,'transport':'messages:'+r.account_id})
                self.state.execute('INSERT INTO inbox VALUES (?,?,?,?)',(guid,fingerprint,envelope,status))
            maximum=max([cutoff]+[row[0] for row in rows])
            self.state.execute("UPDATE meta SET value=? WHERE key='checkpoint'",(str(maximum),))
        return self.state.execute('SELECT guid,status FROM inbox ORDER BY rowid').fetchall()

    def _set_queue_active(self,guid):
        # Durable reservation precedes handle(): a crash cannot let the next
        # request jump past a possibly submitted send.
        with self.state:
            if guid is None:self.state.execute("DELETE FROM meta WHERE key='queue_active'")
            else:self.state.execute("INSERT OR REPLACE INTO meta VALUES ('queue_active',?)",(guid,))

    def _resume_queue(self,relay):
        active=self.state.execute("SELECT value FROM meta WHERE key='queue_active'").fetchone()
        if not active:
            # Upgrade existing journals as well as newly reserved dispatches.
            legacy=self.state.execute("SELECT guid FROM inbox WHERE status IN ('sending','delivery_uncertain','processing') ORDER BY rowid LIMIT 1").fetchone()
            if not legacy:return
            self._set_queue_active(legacy[0]);active=legacy
        row=self.state.execute('SELECT envelope FROM inbox WHERE guid=?',(active[0],)).fetchone()
        if row is None:raise RelayFailure('queue_active_identity_missing')
        # This path is receipt-only. It must never call handle(), the provider,
        # or a transport send while resolving an interrupted reservation.
        try:result=relay.reconcile_delivery(Incoming(**json.loads(row[0])))
        except Exception as exc:raise RelayFailure('queue_stop_reconciliation_required') from exc
        if result['status'] in ('pending','processing','sending','delivery_uncertain'):
            raise RelayFailure('queue_stop_reconciliation_required')
        with self.state:
            self.state.execute('UPDATE inbox SET status=? WHERE guid=?',(result['status'],active[0]))
            self.state.execute("DELETE FROM meta WHERE key='queue_active'")

    def dispatch_pending(self,relay):
        # Serialize the entire gate/claim/dispatch transaction across instances.
        # The runner's lock alone is insufficient for direct adapter callers.
        with open(self.dispatch_lock_path,'a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise RelayFailure('queue_dispatch_busy')
            self._resume_queue(relay)
            results=[]
            for guid,envelope in self.state.execute("SELECT guid,envelope FROM inbox WHERE status='pending' ORDER BY rowid").fetchall():
                self._set_queue_active(guid)
                result=relay.handle(Incoming(**json.loads(envelope)))
                results.append(result)
                # Leave the durable reservation in place for uncertain sends
                # and crash-leftover execution, then stop THIS batch now.
                if result['status'] in ('pending','processing','sending','delivery_uncertain'):
                    with self.state:self.state.execute('UPDATE inbox SET status=? WHERE guid=?',(result['status'],guid))
                    break
                with self.state:
                    self.state.execute('UPDATE inbox SET status=? WHERE guid=?',(result['status'],guid))
                    self.state.execute("DELETE FROM meta WHERE key='queue_active'")
            return results


def payload_hash(reply):
    return hashlib.sha256(json.dumps([reply.account_id,reply.conversation_id,reply.message_id,reply.text,
        hashlib.sha256(reply.image).hexdigest() if reply.image is not None else None],ensure_ascii=False).encode()).hexdigest()


class MessagesSender:
    """Validated transport receipts; timeout receipts require explicit reconciliation.

    transport.send/reconcile never receive a guessed account or recipient.
    Persisted sending/uncertain entries are NOT automatically resent on restart.
    """
    def __init__(self,state_path,route,transport,*,live_enabled=False):
        route.validate()
        if getattr(transport,'offline_fake',False) is not True and not (live_enabled and getattr(transport,'live_enabled',False)):raise RelayFailure('live_sender_disabled')
        self.route=route;self.transport=transport;self.lock=threading.RLock()
        self.db=sqlite3.connect(state_path,check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS outbox (token TEXT PRIMARY KEY,hash TEXT,account TEXT,chat TEXT,status TEXT,receipt TEXT)');self.db.commit()

    def _receipt(self,receipt,token,digest):
        expected={'token':token,'hash':digest,'account':self.route.account_id,'chat':self.route.chat_guid}
        if not isinstance(receipt,dict) or receipt.get('error') or receipt.get('ok') is False or any(receipt.get(k)!=v for k,v in expected.items()) or not receipt.get('message_guid'):
            raise RelayFailure('send_receipt_unverified')
        return receipt

    def __call__(self,reply):
        r=self.route
        if (reply.account_id,reply.conversation_id)!=(r.account_id,r.chat_guid):raise RelayFailure('sender_route_mismatch')
        digest=payload_hash(reply);token=reply.request_id
        with self.lock:
            inserted=self.db.execute('INSERT OR IGNORE INTO outbox VALUES (?,?,?,?,?,NULL)',(token,digest,r.account_id,r.chat_guid,'sending')).rowcount
            self.db.commit()
            if not inserted:
                row=self.db.execute('SELECT hash,account,chat,status FROM outbox WHERE token=?',(token,)).fetchone()
                if row[:3]!=(digest,r.account_id,r.chat_guid):raise RelayFailure('outbox_identity_conflict')
                if row[3]=='submitted':return
                raise RelayFailure('send_uncertain_reconcile_required')
            try:
                receipt=self._receipt(self.transport.send(reply,token,digest),token,digest)
                self.db.execute("UPDATE outbox SET status='submitted',receipt=? WHERE token=?",(json.dumps(receipt),token));self.db.commit()
            except Exception:
                self.db.execute("UPDATE outbox SET status='uncertain' WHERE token=?",(token,));self.db.commit()
                raise RelayFailure('send_uncertain_reconcile_required')

    def reconcile(self,token):
        with self.lock:
            row=self.db.execute('SELECT hash,account,chat,status FROM outbox WHERE token=?',(token,)).fetchone()
            if row is None:raise RelayFailure('unknown_send_token')
            if row[1:3]!=(self.route.account_id,self.route.chat_guid):raise RelayFailure('outbox_identity_conflict')
            if row[3]=='submitted':return 'submitted'
            receipt=self.transport.reconcile(token,row[0],row[1],row[2])
            if receipt is None:return 'uncertain'  # Absence/timeouts never prove non-delivery.
            receipt=self._receipt(receipt,token,row[0])
            self.db.execute("UPDATE outbox SET status='submitted',receipt=? WHERE token=?",(json.dumps(receipt),token));self.db.commit()
            return 'submitted'
