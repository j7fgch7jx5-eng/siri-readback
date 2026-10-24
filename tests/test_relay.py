import base64
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from siri_cli.relay import Incoming,Preview,Relay,RelayFailure,validate_png


def preview(ticket,**kwargs):
    return Preview(ticket,text='Siri answer for '+ticket.incoming.query,scoped=True,fresh_turn=True,identity_verified=True,**kwargs)


def test_delivery_and_persistent_duplicate_idempotence():
    with TemporaryDirectory() as d:
        sent=[];calls=[]
        def provider(t):calls.append(t);return preview(t)
        r=Relay(Path(d)/'db',Path(d)/'out',provider,sent.append)
        incoming=Incoming('m1','chat1','request1')
        assert r.handle(incoming)['status']=='sent'
        assert r.handle(incoming)['status']=='sent'
        r2=Relay(Path(d)/'db',Path(d)/'out',provider,sent.append)
        assert r2.handle(incoming)['status']=='sent'
        assert len(sent)==len(calls)==1
        assert sent[0].message_id=='m1' and sent[0].conversation_id=='chat1'
        assert sent[0].completion=='unverified'
        assert r.handle(replace(incoming,conversation_id='other'))['error']=='message_id_conflict'


def test_concurrency_serializes_siri_and_duplicate_inflight():
    with TemporaryDirectory() as d:
        entered=threading.Event();release=threading.Event();calls=[];sent=[]
        def provider(t):
            calls.append(t.incoming.message_id)
            if t.incoming.message_id=='a':entered.set();assert release.wait(2)
            return preview(t)
        r=Relay(Path(d)/'db',Path(d)/'out',provider,sent.append)
        a=Incoming('a','one','one');b=Incoming('b','two','two')
        t1=threading.Thread(target=r.handle,args=(a,));t1.start();assert entered.wait(2)
        assert r.handle(a)['status']=='processing'
        t2=threading.Thread(target=r.handle,args=(b,));t2.start();time.sleep(.03)
        assert calls==['a'];release.set();t1.join(2);t2.join(2)
        assert not t1.is_alive() and not t2.is_alive()
        assert [(p.message_id,p.conversation_id) for p in sent]==[('a','one'),('b','two')]


def test_cancel_deadline_uncertain_and_stale_never_send_or_retry():
    for failure in ('cancel','deadline','uncertain','stale','wrong_turn','capture'):
        with TemporaryDirectory() as d:
            event=threading.Event();calls=[];sent=[]
            def provider(t):
                calls.append(t)
                if failure=='cancel':event.set()
                if failure=='deadline':time.sleep(.025)
                if failure=='uncertain':raise RelayFailure('submission_uncertain')
                if failure=='capture':raise RelayFailure('capture_failed')
                value=preview(t)
                if failure=='stale':value=replace(value,ticket=replace(t,request_id='old'))
                if failure=='wrong_turn':value=replace(value,identity_verified=False)
                return value
            r=Relay(Path(d)/'db',Path(d)/'out',provider,sent.append)
            incoming=Incoming('m','chat','new',timeout_s=.01 if failure=='deadline' else 2)
            result=r.handle(incoming,event);assert result['status']=='failed'
            assert not sent
            assert r.handle(incoming,event)==result and len(calls)==1


def test_raw_cli_output_is_not_a_preview():
    with TemporaryDirectory() as d:
        sent=[];r=Relay(Path(d)/'db',Path(d)/'out',lambda t:'raw stdout with diagnostics',sent.append)
        assert r.handle(Incoming('m','c','q'))['error']=='wrong_request' and not sent


def test_sender_failure_is_uncertain_and_never_replayed():
    with TemporaryDirectory() as d:
        calls=[]
        def sender(p):calls.append(p);raise OSError('may already have sent')
        r=Relay(Path(d)/'db',Path(d)/'out',preview,sender);incoming=Incoming('m','c','q')
        assert r.handle(incoming)['status']=='delivery_uncertain'
        assert r.handle(incoming)['status']=='delivery_uncertain' and len(calls)==1


def png():
    import struct,zlib
    def chunk(kind,data):return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',2,2,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(b'\0'+b'\xff\0\0'*2+b'\0'+b'\0\xff\0'*2))+chunk(b'IEND',b'')


def test_image_delivery_and_attachment_failures():
    for mode in ('valid','outside','corrupt','missing','unreadable'):
        with TemporaryDirectory() as d:
            sent=[]
            def provider(t):
                path=t.directory/'preview.png';path.write_bytes(png() if mode!='corrupt' else b'log data')
                if mode=='outside':path=Path(d)/'saved-weather.png';path.write_bytes(png())
                if mode=='missing':path.unlink()
                return Preview(t,image=path,scoped=True,fresh_turn=True,identity_verified=True,readable=mode!='unreadable')
            r=Relay(Path(d)/'db',Path(d)/'out',provider,sent.append)
            result=r.handle(Incoming('m','c','q'))
            assert result['status']==('sent' if mode=='valid' else 'failed')
            assert len(sent)==int(mode=='valid')
            if sent:assert sent[0].text is None and sent[0].image==png()


def test_cancelled_queued_request_never_invokes_provider():
    with TemporaryDirectory() as d:
        sent=[];event=threading.Event();event.set()
        r=Relay(Path(d)/'db',Path(d)/'out',lambda t:(_ for _ in ()).throw(AssertionError()),sent.append)
        assert r.handle(Incoming('m','c','q'),event)['error']=='cancelled' and not sent


def test_queued_request_deadline_expires_without_submission():
    with TemporaryDirectory() as d:
        calls=[];r=Relay(Path(d)/'db',Path(d)/'out',lambda t:calls.append(t) or preview(t),lambda p:None)
        r.ui_lock.acquire()
        try:result=r.handle(Incoming('m','c','q',timeout_s=.01))
        finally:r.ui_lock.release()
        assert result['error']=='deadline' and not calls


def test_cancellation_during_send_is_reported_without_retry():
    with TemporaryDirectory() as d:
        event=threading.Event();sent=[]
        def sender(reply):sent.append(reply);event.set()
        r=Relay(Path(d)/'db',Path(d)/'out',preview,sender);incoming=Incoming('m','c','q')
        assert r.handle(incoming,event)['status']=='sent_after_stop'
        assert r.handle(incoming,event)['status']=='sent_after_stop' and len(sent)==1
