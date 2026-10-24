import json,time,threading
from types import SimpleNamespace as N
from unittest.mock import patch
import pytest
from siri_cli.completion_surface import describe_surface
from siri_cli.local_readback import RunRecord
from siri_cli.review_result import main as review
from siri_cli.document_response import read_bound_document
from siri_cli.relay import Ticket,Incoming,RelayFailure


def test_review_reads_verified_cache_without_live_action(tmp_path,capsys):
    record=RunRecord(tmp_path,'run','question');record.phase('submission_intent')
    record.finish(dict(kind='text',text='Exact\nExact 3 ',identity_verified=True,fresh_submission_verified=True))
    with patch('siri_cli.local_readback.perform',side_effect=AssertionError('must not run')):
        assert review([str(tmp_path),'--json'])==0
    result=json.loads(capsys.readouterr().out)
    assert result['text']=='Exact\nExact 3 ' and result['cached'] and result['query_resubmitted'] is False
    (tmp_path/'answer.txt').write_text('changed')
    assert review([str(tmp_path)])==1
    assert capsys.readouterr().err.strip()=='Siri failed. Text me again to retry.'


def test_optional_completion_state_never_becomes_a_signal():
    api=N(AXUIElementCopyAttributeNames=lambda *args:(0,['AXEnabled','AXBusy','AXLiveRegionStatus']),
          AXUIElementCopyAttributeValue=lambda ref,name,*args:(0,False if name=='AXBusy' else True if name=='AXEnabled' else 'private text'),
          AXUIElementCopyActionNames=lambda *args:(0,['AXPress','private custom name']))
    result=describe_surface(api,None)
    assert result['completion']=='unverified' and result['states']['AXBusy']['value'] is False
    assert result['states']['AXElementBusy']=={'supported':False}
    assert 'private' not in json.dumps(result) and result['other_action_count']==1


def test_optional_state_read_error_is_recorded_as_numeric_error():
    api=N(AXUIElementCopyAttributeNames=lambda *args:(-25202,None))
    assert describe_surface(api,None)=={'attribute_names_error':-25202}


def test_observed_refusal_is_not_an_accepted_answer():
    from siri_cli.response_kind import select_readable,SiriReportedError
    from test_turn_scope import n
    response=n(children=[n('AXStaticText','Sorry, I don’t understand.',identifier='modelResponse-richTextParagraph',bounds=(1,1,200,20))],bounds=(0,0,300,100))
    with pytest.raises(SiriReportedError) as caught:select_readable(response,(0,0,500,500))
    assert caught.value.source_message=='Sorry, I don’t understand.'


@pytest.mark.parametrize('mode',['recover','second_failure','process_changed','uncertain_close'])
def test_cleanup_reacquires_reads_but_never_repeats_close(mode):
    import sys
    from siri_cli.document_response import NativeExistingDocumentUI
    from siri_cli.capture import ax
    from siri_cli.driver.base import CaptureError
    doc,main,button=object(),object(),object();actions=[];reads=[];events=[]
    ui=NativeExistingDocumentUI.__new__(NativeExistingDocumentUI);ui.deadline=time.monotonic()+2
    ui.active_bound={'pid':1,'document':doc};ui.verify=lambda b:None;ui.structure_emit=events.append
    def windows(app):
        reads.append(1)
        if len(reads)==1 or mode=='second_failure':raise CaptureError('AX application child role read failed (-25202)')
        return [main]
    ui.backend=N(_find_pid=lambda:2 if mode=='process_changed' else 1,_windows=windows)
    api=N(AXUIElementPerformAction=lambda ref,action:actions.append(ref) or (1 if mode=='uncertain_close' else 0))
    def attr(ref,name):return button if name=='AXCloseButton' else 'AXCloseButton'
    with patch.dict(sys.modules,ApplicationServices=api),patch.object(ax,'app_for_pid',return_value=None),patch.object(ax,'_get_attr_checked',side_effect=attr),patch('siri_cli.capture.transaction._same_ref',lambda a,b:a is b):
        if mode=='recover':ui.close_owned_document();assert ui.active_bound is None
        else:
            with pytest.raises((RelayFailure,CaptureError)):ui.close_owned_document()
    assert actions==[button]
    assert len(reads)==(0 if mode=='uncertain_close' else 1 if mode=='process_changed' else 2)
    if events:assert events[0]['action_repeated'] is False


@pytest.mark.parametrize('mode',['source_changed','cancelled'])
def test_post_extraction_failure_removes_candidate_pdf(tmp_path,mode):
    p=N(count=1);p.change_count=lambda:p.count
    artifact=tmp_path/'candidate.pdf';artifact.write_bytes(b'fixture')
    extracted=[];cancel=threading.Event()
    class UI:
        pasteboard=p
        audit=staticmethod(lambda e:None)
        def open_document(self,b):pass
        def open_share(self,b):pass
        def copy_document(self,b):p.count+=1
        def verify(self,b):
            if extracted:
                if mode=='source_changed':raise RelayFailure('source_changed')
                cancel.set()
    def extract(*args,**kwargs):extracted.append(True);return N(path=artifact)
    ticket=Ticket('run',Incoming('id','local','question'),tmp_path,time.monotonic()+5,cancel)
    with patch('siri_cli.document_response.extract_readable_after_copy',side_effect=extract):
        with pytest.raises(RelayFailure):read_bound_document(ticket,UI(),{},validator_python='unused')
    assert not artifact.exists()
