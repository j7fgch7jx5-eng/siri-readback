"""Opt-in local Messages transport. Importing this module performs no IO.

Apple Events acknowledge execution, not delivery. Durable wire plans and scoped
outbound DB records reconcile timeouts; absence never authorizes a retry.
"""
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
from .relay import RelayFailure

SCRIPT = r'''ObjC.import('Foundation');
function run(argv) {
 try {
  const p=JSON.parse(argv[0]), app=Application('Messages');
  const chats=app.chats.whose({id:p.chat})();
  if(chats.length!==1) throw Error('chat not unique');
  const c=chats[0], a=c.account();
  const handles=c.participants().map(x=>x.handle()).sort();
  if(a.id()!==p.account || a.serviceType()!=='iMessage' || !a.enabled() ||
     JSON.stringify(handles)!==JSON.stringify(p.participants)) throw Error('route mismatch');
  app.send(p.file ? Path(p.file) : p.text,{to:c});
  return JSON.stringify({ok:true});
 } catch(e) { return JSON.stringify({ok:false,error:String(e)}); }
}'''


def execute(plan):
    result=subprocess.run(['/usr/bin/osascript','-l','JavaScript','-e',SCRIPT,json.dumps(plan)],
                          capture_output=True,text=True,timeout=20)
    if result.returncode:raise RelayFailure('apple_event_failed')
    try:response=json.loads(result.stdout)
    except (ValueError,TypeError):raise RelayFailure('apple_event_response_invalid')
    if response!={'ok':True}:raise RelayFailure('apple_event_error_response')
    return response


class AppleMessagesTransport:
    def __init__(self,source,state_dir,route,*,live_enabled=False,executor=execute):
        self.live_enabled=live_enabled;self.source=source;self.route=route;self.executor=executor
        self.root=Path(state_dir);self.root.mkdir(parents=True,exist_ok=True)
        self.journal=sqlite3.connect(self.root/'wire.sqlite',check_same_thread=False)
        self.journal.execute('CREATE TABLE IF NOT EXISTS wire(token TEXT PRIMARY KEY,hash TEXT,plan TEXT)');self.journal.commit()

    def _gate(self):
        if not self.live_enabled:raise RelayFailure('live_messages_disabled')

    def send(self,reply,token,digest):
        self._gate()
        # A cross-process lease covers durable planning, Apple Event and receipt.
        with open(self.root/'wire.lock','a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            if self.journal.execute('SELECT 1 FROM wire WHERE token=?',(token,)).fetchone():
                raise RelayFailure('wire_already_attempted_reconcile_required')
            r=self.route
            if (reply.account_id,reply.conversation_id)!=(r.account_id,r.chat_guid):raise RelayFailure('wire_route_mismatch')
            marker='[Siri preview '+token+' '+digest+']'
            plan={'account':r.account_id,'chat':r.chat_guid,'participants':sorted(r.participants),
                  'baseline':self.source.execute('SELECT COALESCE(MAX(ROWID),0) FROM message').fetchone()[0],
                  'file':None,'text':None,'marker':marker}
            if reply.image is not None:
                if reply.text:raise RelayFailure('multipart_reply_not_supported')
                # File-only reply: request correlation is in its unique filename.
                name='siri-'+hashlib.sha256(token.encode()).hexdigest()+'-'+hashlib.sha256(reply.image).hexdigest()+'.png'
                path=self.root/name
                if path.exists() and path.read_bytes()!=reply.image:raise RelayFailure('wire_file_conflict')
                path.write_bytes(reply.image);plan.update(file=str(path.resolve()),image_hash=hashlib.sha256(reply.image).hexdigest(),filename=name)
            elif reply.text:plan['text']=marker+'\n'+reply.text
            else:raise RelayFailure('empty_wire_reply')
            with self.journal:self.journal.execute('INSERT INTO wire VALUES (?,?,?)',(token,digest,json.dumps(plan)))
            response=self.executor(plan)
            if response!={'ok':True}:raise RelayFailure('apple_event_error_response')
            receipt=self.reconcile(token,digest,r.account_id,r.chat_guid)
            if receipt is None:raise RelayFailure('outgoing_record_not_yet_verified')
            return receipt

    def reconcile(self,token,digest,account,chat):
        self._gate()
        row=self.journal.execute('SELECT hash,plan FROM wire WHERE token=?',(token,)).fetchone()
        if not row:return None
        p=json.loads(row[1]);r=self.route
        if row[0]!=digest or (account,chat)!=(p['account'],p['chat']) or (account,chat)!=(r.account_id,r.chat_guid):
            raise RelayFailure('wire_reconciliation_identity_mismatch')
        # Only exact configured chat/account outgoing records after this plan.
        rows=self.source.execute('''SELECT m.ROWID,m.guid,m.text,m.is_sent,m.error FROM message m
          JOIN chat_message_join j ON j.message_id=m.ROWID JOIN chat c ON c.ROWID=j.chat_id
          WHERE c.guid=? AND c.account_id=? AND c.account_login=? AND c.service_name=?
          AND m.account_guid=? AND m.service=? AND m.is_from_me=1 AND m.ROWID>?''',
          (chat,r.chat_account_id,r.account_login,r.service,r.message_account_guid,r.service,p['baseline'])).fetchall()
        matches=[]
        for rowid,guid,text,sent,error in rows:
            if not guid or sent!=1 or error not in (0,None):continue
            if p['file']:
                attachments=self.source.execute('''SELECT a.transfer_name,a.filename FROM attachment a
                  JOIN message_attachment_join j ON j.attachment_id=a.ROWID WHERE j.message_id=?''',(rowid,)).fetchall()
                if len(attachments)!=1:continue
                name,path=attachments[0]
                if name!=p['filename'] or not path:continue
                try:data=Path(path).expanduser().read_bytes()
                except OSError:continue
                if hashlib.sha256(data).hexdigest()!=p['image_hash']:continue
            elif text!=p['text']:continue
            associations=self.source.execute('SELECT COUNT(DISTINCT chat_id) FROM chat_message_join WHERE message_id=?',(rowid,)).fetchone()[0]
            if associations!=1:raise RelayFailure('outbound_chat_ambiguous')
            matches.append(guid)
        if len(matches)>1:raise RelayFailure('outbound_receipt_ambiguous')
        if not matches:return None
        return dict(token=token,hash=digest,account=account,chat=chat,message_guid=matches[0])
