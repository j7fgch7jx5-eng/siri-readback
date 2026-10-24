from dataclasses import replace
from pathlib import Path
import sqlite3
import threading
from tempfile import TemporaryDirectory
from unittest.mock import patch
from siri_cli.messages_adapter import Route,MessagesInbox,MessagesSender
from siri_cli.preview_provider import SiriPreviewProvider,NativePreviewPort,Turn
from siri_cli.relay import Relay,Incoming,Preview,RelayFailure
from test_turn_scope import fails


def route():
    return Route('fixture-account','opaque-chat','db-account-id','fixture-login','opaque-account-guid',
                 frozenset({'remote-fixture'}),frozenset({'self-fixture'}))


def database():
    c=sqlite3.connect(':memory:')
    c.executescript('''CREATE TABLE relay_fixture (id);
    CREATE TABLE chat (guid,account_id,account_login,service_name);
    CREATE TABLE handle (id);
    CREATE TABLE chat_handle_join(chat_id,handle_id);
    CREATE TABLE chat_message_join(chat_id,message_id);
    CREATE TABLE message(guid,text,is_from_me,is_system_message,date_edited,date_retracted,account_guid,service,handle_id);
    INSERT INTO chat VALUES ('opaque-chat','db-account-id','fixture-login','iMessage');
    INSERT INTO handle VALUES ('remote-fixture');
    INSERT INTO handle VALUES ('self-fixture');
    INSERT INTO chat_handle_join VALUES (1,1);''')
    return c


def add(c,guid,body='new fixture request',*,outbound=0,system=0,edited=0,retracted=0,account='opaque-account-guid',sender=1):
    cursor=c.execute('INSERT INTO message VALUES (?,?,?,?,?,?,?,?,?)',(guid,body,outbound,system,edited,retracted,account,'iMessage',sender))
    rowid=cursor.lastrowid;c.execute('INSERT INTO chat_message_join VALUES (1,?)',(rowid,));c.commit();return rowid


class Port:
    def __init__(self,mode='ok'):self.mode=mode;self.submissions=[]
    def prepare(self,t):
        self.ticket=t;self.calls=0;self.refs=(1,object(),object(),object(),object());self.prompt=object();self.response=object()
        return Turn((*self.refs,None,None),'',[],0,1 if self.mode=='old' else 0)
    def submit(self,t,before):
        self.submissions.append(t.incoming.query)
        if self.mode=='uncertain':raise TimeoutError('AX confirm may have happened')
    def observe(self,t):
        self.calls+=1
        if self.mode=='deadline' or self.calls==1:return Turn((*self.refs,None,self.response),'',[],1)
        identity=(*self.refs,self.prompt,self.response)
        if self.mode=='wrong':return Turn(identity,'',['different'],1,ready=True)
        if self.mode=='replaced' and self.calls>2:identity=(*self.refs,self.prompt,object())
        return Turn(identity,'',[t.incoming.query],1,ready=True,fingerprint='changed' if self.mode=='mutated' and self.calls>2 else 'answer')
    def pause(self,t):
        if self.mode=='deadline':t.cancel.set()
    def capture(self,t,turn):
        if self.mode=='capture_failure':raise RelayFailure('capture_failed')
        return Preview(t,text='FAKE RESPONSE: '+t.incoming.query,scoped=True,fresh_turn=True,identity_verified=True)


class Transport:
    offline_fake=True
    def __init__(self,timeout=False):self.sent=[];self.receipts={};self.timeout=timeout
    def send(self,reply,token,digest):
        self.sent.append(reply)
        receipt={'token':token,'hash':digest,'account':reply.account_id,'chat':reply.conversation_id,'message_guid':'opaque-outbound-'+token}
        self.receipts[token]=receipt
        if self.timeout:raise TimeoutError('already delivered to transport')
        return receipt
    def reconcile(self,token,digest,account,chat):return self.receipts.get(token)


def test_wired_database_provider_sender_path_and_restart_deduplication():
    with TemporaryDirectory() as d,patch('siri_cli.capture.transaction._same_ref',lambda a,b:a is b):
        c=database();add(c,'old','SAVED WEATHER MUST NOT BE SENT')
        inbox=MessagesInbox(c,Path(d)/'inbox',route());port=Port();transport=Transport()
        sender=MessagesSender(Path(d)/'outbox',route(),transport)
        relay=Relay(Path(d)/'relay',Path(d)/'artifacts',SiriPreviewProvider(port),sender)
        row=add(c,'opaque-incoming','fresh synthetic A');c.execute('INSERT INTO chat_message_join VALUES (1,?)',(row,));c.commit()
        inbox.poll_once();assert inbox.dispatch_pending(relay)[0]['status']=='sent'
        inbox=MessagesInbox(c,Path(d)/'inbox',route());inbox.poll_once();assert inbox.dispatch_pending(relay)==[]
        assert port.submissions==['fresh synthetic A'] and len(transport.sent)==1
        reply=transport.sent[0]
        assert (reply.account_id,reply.conversation_id,reply.message_id)==('fixture-account','opaque-chat','opaque-incoming')
        assert reply.text=='FAKE RESPONSE: fresh synthetic A' and reply.completion=='unverified'


def test_provider_failure_modes_never_send_or_repeat_uncertain_submission():
    for mode in ('old','uncertain','deadline','wrong','replaced','mutated','capture_failure'):
        with TemporaryDirectory() as d,patch('siri_cli.capture.transaction._same_ref',lambda a,b:a is b):
            port=Port(mode);sent=[];relay=Relay(Path(d)/'db',Path(d)/'files',SiriPreviewProvider(port),sent.append)
            incoming=Incoming('m','c','new request')
            result=relay.handle(incoming);assert result['status']=='failed' and not sent
            assert relay.handle(incoming)==result and len(port.submissions)==int(mode!='old')


def test_inbox_excludes_outbound_self_system_edits_and_bad_identity():
    with TemporaryDirectory() as d:
        c=database();inbox=MessagesInbox(c,Path(d)/'state',route())
        add(c,'out',outbound=1);add(c,'self',sender=2);add(c,'system',system=1)
        add(c,'edited',edited=1);add(c,'wrong-account',account='another')
        add(c,'unsupported',body=None);add(c,'ok')
        statuses=dict(inbox.poll_once())
        assert statuses=={'out':'excluded','self':'excluded','system':'excluded','edited':'edited_blocked',
                          'wrong-account':'identity_blocked','unsupported':'unsupported_body','ok':'pending'}
        c.execute("UPDATE message SET text='changed',date_edited=2 WHERE guid='ok'");c.commit()
        assert dict(inbox.poll_once())['ok']=='edited_blocked'


def test_guid_collision_and_mapping_changes_are_not_guessed():
    with TemporaryDirectory() as d:
        c=database();inbox=MessagesInbox(c,Path(d)/'s',route())
        add(c,'same','first');add(c,'same','different')
        assert dict(inbox.poll_once())['same']=='edited_blocked'
        c.execute("UPDATE chat SET account_id='other'");c.commit()
        fails(inbox.poll_once,RelayFailure)
        fails(lambda:MessagesInbox(c,Path(d)/'s',replace(route(),account_id='other')),RelayFailure)


def test_receipt_timeout_and_restart_reconcile_without_resending_or_siri_retry():
    with TemporaryDirectory() as d,patch('siri_cli.capture.transaction._same_ref',lambda a,b:a is b):
        transport=Transport(timeout=True);port=Port();incoming=Incoming('m','opaque-chat','new',transport='messages:fixture-account',account_id='fixture-account')
        def make():
            sender=MessagesSender(Path(d)/'outbox',route(),transport)
            return Relay(Path(d)/'relay',Path(d)/'artifacts',SiriPreviewProvider(port),sender)
        relay=make();assert relay.handle(incoming)['status']=='delivery_uncertain'
        relay=make();assert relay.handle(incoming)['status']=='delivery_uncertain'
        assert relay.reconcile_delivery(incoming)['status']=='sent_reconciled'
        assert len(transport.sent)==1 and port.submissions==['new']


def test_missing_or_wrong_receipt_never_enables_resend():
    with TemporaryDirectory() as d,patch('siri_cli.capture.transaction._same_ref',lambda a,b:a is b):
        transport=Transport(timeout=True);port=Port();incoming=Incoming('m','opaque-chat','new',account_id='fixture-account')
        sender=MessagesSender(Path(d)/'o',route(),transport);relay=Relay(Path(d)/'r',Path(d)/'f',SiriPreviewProvider(port),sender)
        relay.handle(incoming);receipt=next(iter(transport.receipts.values()));token=receipt['token']
        transport.receipts.clear();assert relay.reconcile_delivery(incoming)['status']=='delivery_uncertain'
        transport.receipts[token]={**receipt,'chat':'wrong'}
        assert relay.reconcile_delivery(incoming)['status']=='delivery_uncertain'
        assert len(transport.sent)==1 and len(port.submissions)==1


def test_live_operations_are_disabled_without_reading_bodies_or_sending():
    with TemporaryDirectory() as d:
        port=NativePreviewPort()
        fails(lambda:port.prepare(None),RelayFailure)
        c=sqlite3.connect(':memory:')
        fails(lambda:MessagesInbox(c,Path(d)/'state',route()),RelayFailure)
        fails(lambda:MessagesSender(Path(d)/'out',route(),object()),RelayFailure)


def test_duplicate_chat_guid_and_changed_participants_are_rejected():
    with TemporaryDirectory() as d:
        c=database();c.execute("INSERT INTO chat SELECT * FROM chat");c.commit()
        fails(lambda:MessagesInbox(c,Path(d)/'s',route()),RelayFailure)
        c.execute('DELETE FROM chat WHERE ROWID=2');c.execute('DELETE FROM chat_handle_join');c.commit()
        fails(lambda:MessagesInbox(c,Path(d)/'s',route()),RelayFailure)


def test_durable_spool_restart_and_crash_after_relay_return_are_safe():
    import json
    with TemporaryDirectory() as d,patch('siri_cli.capture.transaction._same_ref',lambda a,b:a is b):
        c=database();add(c,'history','old request');inbox=MessagesInbox(c,Path(d)/'i',route())
        add(c,'fresh');inbox.poll_once()
        inbox=MessagesInbox(c,Path(d)/'i',route())
        envelope=inbox.state.execute("SELECT envelope FROM inbox WHERE guid='fresh'").fetchone()[0]
        port=Port();transport=Transport();sender=MessagesSender(Path(d)/'o',route(),transport)
        relay=Relay(Path(d)/'r',Path(d)/'f',SiriPreviewProvider(port),sender)
        assert relay.handle(Incoming(**json.loads(envelope)))['status']=='sent'
        # Simulated crash before inbox marks the already handled message done.
        inbox=MessagesInbox(c,Path(d)/'i',route());inbox.poll_once();inbox.dispatch_pending(relay)
        assert len(transport.sent)==1 and len(port.submissions)==1
        assert inbox.state.execute("SELECT value FROM meta WHERE key='checkpoint'").fetchone()[0]=='2'


def test_identical_duplicate_guid_rows_do_not_duplicate_requests():
    with TemporaryDirectory() as d:
        c=database();inbox=MessagesInbox(c,Path(d)/'s',route());add(c,'same');add(c,'same')
        assert inbox.poll_once()==[('same','pending')]
        c.execute('INSERT INTO chat VALUES (?,?,?,?)',('other','db-account-id','fixture-login','iMessage'))
        c.execute('INSERT INTO chat_message_join VALUES (2,1)');c.commit()
        fails(inbox.poll_once,RelayFailure)


def test_cutoff_identity_and_rowid_reset_cannot_silently_replay_history():
    with TemporaryDirectory() as d:
        c=database();add(c,'anchor');inbox=MessagesInbox(c,Path(d)/'s',route())
        c.execute("UPDATE message SET guid='replacement' WHERE ROWID=1");c.commit()
        fails(inbox.poll_once,RelayFailure)


def test_sending_crash_state_requires_receipt_reconciliation_after_restart():
    with TemporaryDirectory() as d,patch('siri_cli.capture.transaction._same_ref',lambda a,b:a is b):
        transport=Transport(timeout=True);incoming=Incoming('m','opaque-chat','new',account_id='fixture-account')
        sender=MessagesSender(Path(d)/'o',route(),transport)
        relay=Relay(Path(d)/'r',Path(d)/'f',SiriPreviewProvider(Port()),sender)
        result=relay.handle(incoming)
        sender.db.execute("UPDATE outbox SET status='sending'");sender.db.commit()
        relay.db.execute("UPDATE requests SET status='sending'");relay.db.commit()
        restarted=Relay(Path(d)/'r',Path(d)/'f',lambda t:(_ for _ in ()).throw(AssertionError()),
                        MessagesSender(Path(d)/'o',route(),transport))
        assert restarted.reconcile_delivery(incoming)['status']=='sent_reconciled'
        assert len(transport.sent)==1
