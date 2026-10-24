"""Exercise real transport code using synthetic DB records and process responses."""
import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from subprocess import CompletedProcess,TimeoutExpired
from siri_cli.messages_live import AppleMessagesTransport,execute
from siri_cli.messages_adapter import MessagesSender,payload_hash
from siri_cli.relay import Reply,RelayFailure
from siri_cli.relay_live import run
from test_live_adapters_offline import route,database,add
from test_turn_scope import fails


def reply():return Reply('run-token','incoming-guid','opaque-chat','fresh Siri text',None,account_id='fixture-account')


def source():
    c=database();c.execute('ALTER TABLE message ADD COLUMN is_sent');c.execute('ALTER TABLE message ADD COLUMN error');return c


def outgoing(c,p,*,error=0,sent=1):
    cur=c.execute('INSERT INTO message(guid,text,is_from_me,is_system_message,date_edited,date_retracted,account_guid,service,handle_id,is_sent,error) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
        ('actual-outbound-guid',p['text'],1,0,0,0,'opaque-account-guid','iMessage',2,sent,error))
    c.execute('INSERT INTO chat_message_join VALUES (1,?)',(cur.lastrowid,));c.commit()


def test_actual_script_error_and_process_responses_are_not_receipts():
    for result in [CompletedProcess([],0,'{"ok":false,"error":"no chat"}',''),CompletedProcess([],1,'{"ok":true}','error'),CompletedProcess([],0,'undefined',''),CompletedProcess([],0,'{"ok":true,"error":"failure"}','')]:
        with patch('siri_cli.messages_live.subprocess.run',return_value=result):fails(lambda:execute({}),RelayFailure)
    with patch('siri_cli.messages_live.subprocess.run',return_value=CompletedProcess([],0,'{"ok":true}','')):
        assert execute({})=={'ok':True}


def test_real_transport_timeout_restart_reconciles_without_second_send():
    with TemporaryDirectory() as d:
        c=source();calls=[]
        def executor(p):calls.append(p);outgoing(c,p);raise TimeoutError('after send')
        wire=AppleMessagesTransport(c,Path(d)/'wire',route(),live_enabled=True,executor=executor)
        sender=MessagesSender(Path(d)/'sender',route(),wire,live_enabled=True)
        fails(lambda:sender(reply()),RelayFailure)
        wire=AppleMessagesTransport(c,Path(d)/'wire',route(),live_enabled=True,executor=lambda p:1/0)
        restarted=MessagesSender(Path(d)/'sender',route(),wire,live_enabled=True)
        assert restarted.reconcile('run-token')=='submitted'
        restarted(reply());assert len(calls)==1


def test_real_transport_missing_error_and_duplicate_records_remain_uncertain():
    for mode in ('missing','error','duplicate','nonthrowing'):
        with TemporaryDirectory() as d:
            c=source()
            def executor(p):
                if mode!='missing':outgoing(c,p,error=1 if mode=='error' else 0)
                if mode=='duplicate':outgoing(c,p)
                return {'ok':False} if mode=='nonthrowing' else {'ok':True}
            wire=AppleMessagesTransport(c,d,route(),live_enabled=True,executor=executor)
            fails(lambda:wire.send(reply(),'run-token',payload_hash(reply())),RelayFailure)
            fails(lambda:wire.send(reply(),'run-token',payload_hash(reply())),RelayFailure)


def test_real_transport_multiple_instances_atomic_send_claim():
    with TemporaryDirectory() as d:
        c=source();calls=[]
        def executor(p):calls.append(1);outgoing(c,p);return {'ok':True}
        first=AppleMessagesTransport(c,d,route(),live_enabled=True,executor=executor)
        second=AppleMessagesTransport(c,d,route(),live_enabled=True,executor=executor)
        first.send(reply(),'run-token',payload_hash(reply()))
        fails(lambda:second.send(reply(),'run-token',payload_hash(reply())),RelayFailure)
        assert len(calls)==1 and second.reconcile('run-token',payload_hash(reply()),'fixture-account','opaque-chat')


def test_live_switches_fail_before_source_reads_or_executor():
    with TemporaryDirectory() as d:
        wire=AppleMessagesTransport(None,d,route(),executor=lambda p:1/0)
        fails(lambda:wire.send(reply(),'run-token','hash'),RelayFailure)
        fails(lambda:wire.reconcile('run-token','hash','a','b'),RelayFailure)
        fails(lambda:run({},live=True),RelayFailure)
        fails(lambda:run({'live_test_approved':True}),RelayFailure)


def test_pixel_review_uses_actual_image_ocr_and_rejects_wrong_title():
    from siri_cli.pixel_review import PixelReviewer
    class T:
        def check(self):pass
    with TemporaryDirectory() as d:
        p=Path(d)/'actual.png';p.write_bytes(b'current pixels')
        with patch('siri_cli.pixel_review.validate_png'):
            good=PixelReviewer(lambda path:[('Example City 66 partly cloudy',.99)])
            assert good(p,T(),'Example City')==hashlib.sha256(p.read_bytes()).hexdigest()
            fails(lambda:good(p,T(),'Miami'),RelayFailure)
            fails(lambda:PixelReviewer(lambda path:[('Example City',.2)])(p,T(),'Example City'),RelayFailure)


def test_nonempty_preparation_refuses_composer_interference_before_menu():
    from siri_cli.preview_provider import NativePreviewPort,Turn
    class T:
        def check(self):pass
    port=NativePreviewPort();port.observe=lambda t:Turn((1,2,3,4,5,None,None),'user typing',[],1,2)
    fails(lambda:port.prepare_empty(T()),RelayFailure)


def test_attachment_receipt_verifies_bytes_and_restart_recovery():
    with TemporaryDirectory() as d:
        c=source();c.executescript('CREATE TABLE attachment(transfer_name,filename); CREATE TABLE message_attachment_join(message_id,attachment_id);')
        item=replace(reply(),text=None,image=b'synthetic attachment bytes')
        def executor(p):
            outgoing(c,p)
            c.execute('INSERT INTO attachment VALUES (?,?)',(p['filename'],p['file']))
            c.execute('INSERT INTO message_attachment_join VALUES (1,1)');c.commit()
            raise TimeoutError()
        wire=AppleMessagesTransport(c,d,route(),live_enabled=True,executor=executor)
        digest=payload_hash(item)
        fails(lambda:wire.send(item,item.request_id,digest),TimeoutError)
        restarted=AppleMessagesTransport(c,d,route(),live_enabled=True)
        assert restarted.reconcile(item.request_id,digest,item.account_id,item.conversation_id)['message_guid']=='actual-outbound-guid'
        path=Path(c.execute('SELECT filename FROM attachment').fetchone()[0]);path.write_bytes(b'wrong')
        assert restarted.reconcile(item.request_id,digest,item.account_id,item.conversation_id) is None


def test_simultaneous_transport_instances_only_one_executes():
    import threading
    with TemporaryDirectory() as d:
        dbpath=Path(d)/'source.sqlite'
        c=source();dest=sqlite3.connect(dbpath);c.backup(dest);dest.close()
        calls=[];errors=[];barrier=threading.Barrier(2)
        def worker():
            source_db=sqlite3.connect(dbpath)
            wire=AppleMessagesTransport(source_db,Path(d)/'wire',route(),live_enabled=True,
                executor=lambda p:(calls.append(p) or {'ok':True}))
            barrier.wait()
            try:wire.send(reply(),'run-token',payload_hash(reply()))
            except RelayFailure as e:errors.append(str(e))
        threads=[threading.Thread(target=worker) for _ in range(2)]
        for t in threads:t.start()
        for t in threads:t.join(5)
        assert not any(t.is_alive() for t in threads)
        assert len(calls)==1 and len(errors)==2
        assert 'wire_already_attempted_reconcile_required' in errors


def test_real_runner_restart_reconciles_core_and_never_requeries():
    from dataclasses import asdict
    from test_live_adapters_offline import Port
    with TemporaryDirectory() as d:
        dbpath=Path(d)/'chat.sqlite';c=source();dest=sqlite3.connect(dbpath);c.backup(dest);dest.close()
        c=sqlite3.connect(dbpath);port=Port();calls=[]
        config={'live_test_approved':True,'database':str(dbpath),'state_dir':str(Path(d)/'state'),'route':asdict(route())}
        def factory(source_db,root,r,**kwargs):
            def executor(p):
                calls.append(p);outgoing(c,p);raise TimeoutError('send happened before timeout')
            return AppleMessagesTransport(source_db,root,r,executor=executor,**kwargs)
        with patch('siri_cli.relay_live.NativePreviewPort',return_value=port),patch('siri_cli.relay_live.AppleMessagesTransport',side_effect=factory),patch('siri_cli.capture.transaction._same_ref',lambda a,b:a is b):
            assert run(config,live=True)==[]
            cur=c.execute("INSERT INTO message(guid,text,is_from_me,is_system_message,date_edited,date_retracted,account_guid,service,handle_id) VALUES ('new-guid','fresh weather fixture',0,0,0,0,'opaque-account-guid','iMessage',1)")
            c.execute('INSERT INTO chat_message_join VALUES (1,?)',(cur.lastrowid,));c.commit()
            assert run(config,live=True)[0]['status']=='delivery_uncertain'
            assert run(config,live=True)==[]
            assert len(calls)==1 and port.submissions==['fresh weather fixture']
            journal=sqlite3.connect(Path(d)/'state'/'relay.sqlite')
            assert journal.execute('SELECT status FROM requests').fetchone()[0]=='sent_reconciled'


def test_nonempty_preparation_uses_one_new_conversation_without_history_reads():
    from contextlib import nullcontext
    from types import SimpleNamespace
    from siri_cli.preview_provider import NativePreviewPort,Turn
    class T:
        deadline=10**12
        def check(self):pass
    for mode in ('ok','window_replaced','nonempty_new_pane'):
        port=NativePreviewPort();refs=(1,object(),object(),object(),object(),None,None);port.base=refs;port.pid=1;actions=[]
        after=list(refs)
        if mode=='window_replaced':after[1]=object()
        if mode=='nonempty_new_pane':after[2]=object()
        old=Turn(refs,'',[],1,2);new=Turn(tuple(after),'',[],0 if mode!='nonempty_new_pane' else 1,0 if mode!='nonempty_new_pane' else 2)
        sequence=iter([old,old,new])
        port.observe=lambda t:next(sequence)
        port.backend=SimpleNamespace(_open_new_conversation_menu=lambda app:actions.append(app) or True)
        with patch('siri_cli.capture.ax.app_for_pid',return_value='app'),patch('siri_cli.capture.deadline.bounded_ax_reads',return_value=nullcontext()),patch('siri_cli.preview_provider._same_ref',lambda a,b:a is b):
            if mode=='ok':assert port.prepare_empty(T()) is new
            else:fails(lambda:port.prepare_empty(T()),RelayFailure)
        assert actions==['app']


def test_native_ocr_uses_foundation_options_not_python_mapping():
    from types import SimpleNamespace
    from siri_cli.pixel_review import vision_lines
    options=object();calls=[]
    handler=SimpleNamespace(performRequests_error_=lambda requests,error:(True,None))
    class Alloc:
        def alloc(self):return self
        def initWithURL_options_(self,url,opts):
            assert opts is options;calls.append(url);return handler
    request=SimpleNamespace(setRecognitionLevel_=lambda level:None,results=lambda:[])
    foundation=SimpleNamespace(NSDictionary=SimpleNamespace(dictionary=lambda:options),NSURL=SimpleNamespace(fileURLWithPath_=lambda p:p))
    vision=SimpleNamespace(VNImageRequestHandler=Alloc(),VNRecognizeTextRequest=SimpleNamespace(alloc=lambda:SimpleNamespace(init=lambda:request)),VNRequestTextRecognitionLevelAccurate=0)
    with patch.dict('sys.modules',{'Foundation':foundation,'Vision':vision}):
        assert vision_lines(Path('/synthetic/current.png'))==[]
    assert calls==['/synthetic/current.png']


def test_two_pending_requests_stop_immediately_and_across_restart_until_receipt():
    import json
    from siri_cli.messages_adapter import MessagesInbox
    from siri_cli.preview_provider import SiriPreviewProvider
    from siri_cli.relay import Relay
    from test_live_adapters_offline import Port,Transport
    with TemporaryDirectory() as d,patch('siri_cli.capture.transaction._same_ref',lambda a,b:a is b):
        c=database();inbox=MessagesInbox(c,Path(d)/'inbox',route());add(c,'A','request A');add(c,'B','request B');inbox.poll_once()
        port=Port();transport=Transport(timeout=True)
        def make_relay():return Relay(Path(d)/'relay',Path(d)/'artifacts',SiriPreviewProvider(port),MessagesSender(Path(d)/'sender',route(),transport))
        relay=make_relay();results=inbox.dispatch_pending(relay)
        assert len(results)==1 and results[0]['status']=='delivery_uncertain'
        assert port.submissions==['request A'] and len(transport.sent)==1
        assert inbox.state.execute("SELECT status FROM inbox WHERE guid='B'").fetchone()[0]=='pending'
        receipts=dict(transport.receipts);transport.receipts.clear()
        fails(lambda:inbox.dispatch_pending(relay),RelayFailure)
        assert port.submissions==['request A']
        # Both durable adapters are reconstructed. No receipt => still stopped.
        inbox=MessagesInbox(c,Path(d)/'inbox',route());relay=make_relay()
        fails(lambda:inbox.dispatch_pending(relay),RelayFailure)
        assert port.submissions==['request A'] and len(transport.sent)==1
        # Receipt-only recovery releases A; B can now be handled exactly once.
        transport.receipts.update(receipts);transport.timeout=False
        assert inbox.dispatch_pending(relay)[0]['status']=='sent'
        assert port.submissions==['request A','request B'] and len(transport.sent)==2
        assert inbox.state.execute("SELECT status FROM inbox WHERE guid='A'").fetchone()[0]=='sent_reconciled'
        assert inbox.dispatch_pending(relay)==[]


def test_queue_reservation_survives_crash_after_uncertain_handle_before_spool_update():
    from siri_cli.messages_adapter import MessagesInbox
    from siri_cli.preview_provider import SiriPreviewProvider
    from siri_cli.relay import Relay
    from test_live_adapters_offline import Port,Transport
    with TemporaryDirectory() as d,patch('siri_cli.capture.transaction._same_ref',lambda a,b:a is b):
        c=database();inbox=MessagesInbox(c,Path(d)/'inbox',route());add(c,'A','request A');add(c,'B','request B');inbox.poll_once()
        port=Port();transport=Transport(timeout=True)
        relay=Relay(Path(d)/'relay',Path(d)/'artifacts',SiriPreviewProvider(port),MessagesSender(Path(d)/'sender',route(),transport))
        real_handle=relay.handle
        def crash(incoming):real_handle(incoming);raise RuntimeError('crash before spool verdict commit')
        relay.handle=crash
        fails(lambda:inbox.dispatch_pending(relay),RuntimeError)
        assert inbox.state.execute("SELECT value FROM meta WHERE key='queue_active'").fetchone()[0]=='A'
        transport.receipts.clear()
        inbox=MessagesInbox(c,Path(d)/'inbox',route());relay.handle=real_handle
        fails(lambda:inbox.dispatch_pending(relay),RelayFailure)
        assert port.submissions==['request A'] and len(transport.sent)==1


def test_real_runner_two_pending_and_restart_blocks_until_outbound_record():
    from dataclasses import asdict
    from test_live_adapters_offline import Port
    with TemporaryDirectory() as d:
        dbpath=Path(d)/'chat.sqlite';c=source();dest=sqlite3.connect(dbpath);c.backup(dest);dest.close()
        c=sqlite3.connect(dbpath);port=Port();calls=[]
        config={'live_test_approved':True,'database':str(dbpath),'state_dir':str(Path(d)/'state'),'route':asdict(route())}
        def factory(source_db,root,r,**kwargs):
            def executor(plan):calls.append(plan);return {'ok':True}
            return AppleMessagesTransport(source_db,root,r,executor=executor,**kwargs)
        with patch('siri_cli.relay_live.NativePreviewPort',return_value=port),patch('siri_cli.relay_live.AppleMessagesTransport',side_effect=factory),patch('siri_cli.capture.transaction._same_ref',lambda a,b:a is b):
            assert run(config,live=True)==[]
            for guid in ('A','B'):
                cur=c.execute("INSERT INTO message(guid,text,is_from_me,is_system_message,date_edited,date_retracted,account_guid,service,handle_id) VALUES (?,?,0,0,0,0,'opaque-account-guid','iMessage',1)",(guid,'fresh '+guid))
                c.execute('INSERT INTO chat_message_join VALUES (1,?)',(cur.lastrowid,))
            c.commit()
            assert len(run(config,live=True))==1
            assert port.submissions==['fresh A'] and len(calls)==1
            fails(lambda:run(config,live=True),RelayFailure)
            assert port.submissions==['fresh A'] and len(calls)==1
            outgoing(c,calls[0])
            # A is receipt-reconciled, then B is submitted once and itself stops.
            assert run(config,live=True)[0]['status']=='delivery_uncertain'
            assert port.submissions==['fresh A','fresh B'] and len(calls)==2


def test_adapter_dispatch_lease_prevents_parallel_instance_from_crossing_gate():
    import threading
    from types import SimpleNamespace
    from siri_cli.messages_adapter import MessagesInbox
    with TemporaryDirectory() as d:
        c=database();inbox=MessagesInbox(c,Path(d)/'inbox',route());add(c,'A');inbox.poll_once()
        entered=threading.Event();release=threading.Event();errors=[]
        def handle(incoming):entered.set();assert release.wait(5);return {'status':'sent'}
        relay=SimpleNamespace(handle=handle)
        def worker():
            try:MessagesInbox(database(),Path(d)/'inbox',route()).dispatch_pending(relay)
            except Exception as e:errors.append(e)
        thread=threading.Thread(target=worker);thread.start()
        try:
            assert entered.wait(5)
            try:inbox.dispatch_pending(relay)
            except RelayFailure as e:assert str(e)=='queue_dispatch_busy'
            else:raise AssertionError('parallel dispatch crossed reservation')
        finally:release.set();thread.join(5)
        assert not thread.is_alive() and not errors


def test_legacy_uncertain_spool_is_upgraded_to_persistent_queue_stop():
    from types import SimpleNamespace
    from siri_cli.messages_adapter import MessagesInbox
    with TemporaryDirectory() as d:
        c=database();inbox=MessagesInbox(c,Path(d)/'inbox',route());add(c,'A');add(c,'B');inbox.poll_once()
        inbox.state.execute("UPDATE inbox SET status='delivery_uncertain' WHERE guid='A'");inbox.state.commit()
        relay=SimpleNamespace(reconcile_delivery=lambda incoming:{'status':'delivery_uncertain'},handle=lambda incoming:(_ for _ in ()).throw(AssertionError('B must not run')))
        fails(lambda:inbox.dispatch_pending(relay),RelayFailure)
        assert inbox.state.execute("SELECT value FROM meta WHERE key='queue_active'").fetchone()[0]=='A'
