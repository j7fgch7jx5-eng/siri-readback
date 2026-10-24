import fcntl,json,os,threading,time
from types import SimpleNamespace as N
from unittest.mock import patch
import pytest
from siri_cli.local_readback import RunRecord,main
from siri_cli.local_port import AXRecovery,LocalReadablePort
from siri_cli.relay import RelayFailure
from siri_cli.capture import ax
from test_turn_scope import fixture,n


@pytest.mark.parametrize('mode',['recover','second_failure','wrong_process','wrong_window','unsupported_error','cancel'])
def test_bounded_reacquisition_never_resubmits_or_resets_identity(mode):
    window=object();calls=[];events=[]
    backend=N(_find_pid=lambda:2 if mode=='wrong_process' else 1,_windows=lambda app:[object() if mode=='wrong_window' else window])
    recovery=AXRecovery(backend,1,window,events.append)
    def read():
        calls.append(1)
        if len(calls)==1 or mode=='second_failure':raise ax.AXReadError('AXUIElementCopyAttributeNames',-25205 if mode=='unsupported_error' else -25202)
        return 'complete new snapshot'
    def check():
        if mode=='cancel':raise RelayFailure('cancelled')
    with patch.object(ax,'app_for_pid',return_value=None),patch('siri_cli.local_port._same_ref',lambda a,b:a is b):
        if mode=='recover':assert recovery.call(read,check)=='complete new snapshot'
        else:
            with pytest.raises((RelayFailure,ax.AXReadError)):recovery.call(read,check)
    assert len(calls)==(2 if mode in ('recover','second_failure') else 1)
    if events:assert events[0]['submission_repeated'] is False


@pytest.mark.parametrize('mode',['ok','wrong_prompt','duplicate','wrong_response','wrong_window','incomplete','clipped','cancel'])
def test_actual_local_port_checks_structure_before_response_text(mode):
    paragraph=n('AXStaticText','4\n4',identifier='modelResponse-richTextParagraph',bounds=(5,5,200,30))
    root,c,pane,t,_=fixture(prompt='wrong' if mode=='wrong_prompt' else 'question',answer=[paragraph])
    response=t.children[1];response.element.bounds=(0,0,400,300);t.element.bounds=(0,0,500,500)
    if mode=='duplicate':t.children.append(n(children=[n('AXStaticText','other')],identifier='modelResponse'))
    if mode=='incomplete':root.complete=False
    if mode=='clipped':paragraph.element.bounds=(5,500,200,30)
    for node in ax.nodes(root):node.ref=object()
    win=next(node.ref for node in ax.nodes(root) if node.element.role=='AXWindow');p=t.children[0]
    port=LocalReadablePort(live_enabled=False);port.pid=1;port.submitted=True
    port.base=(1,win,pane.ref,c.ref,t.ref,None,None)
    port.accepted=(1,win,pane.ref,c.ref,t.ref,p.ref,object() if mode=='wrong_response' else response.ref)
    port.backend=N(_find_pid=lambda:1,_windows=lambda app:[object() if mode=='wrong_window' else win])
    reads=[]
    def snapshot(ref,**kw):
        if ref is win:return root
        reads.append(ref)
        return next(node for node in ax.nodes(root) if node.ref is ref)
    def check():
        if mode=='cancel':raise RelayFailure('cancelled')
    ticket=N(check=check,incoming=N(query='question'))
    with patch.object(ax,'app_for_pid',return_value=None),patch.object(ax,'snapshot_tree',side_effect=snapshot),patch.object(ax,'_get_attr_checked',return_value=''),patch.object(ax,'_element_bounds',return_value=t.element.bounds),patch('siri_cli.local_port._same_ref',lambda a,b:a is b):
        if mode=='ok':assert port.snapshot_target(ticket)[0]['kind']=='text-candidate'
        else:
            with pytest.raises((RelayFailure,ValueError)):port.snapshot_target(ticket)
    if mode in ('wrong_prompt','duplicate','wrong_response','wrong_window','incomplete','cancel'):assert response.ref not in reads


def candidate(**kw):return dict(kind='text',text='yes\nyes\n3',identity_verified=True,fresh_submission_verified=True,**kw)


def test_restart_completed_text_is_exact_without_gui(tmp_path,capsys):
    record=RunRecord(tmp_path,'request1','question');record.phase('submission_intent',submission_attempts=1)
    result=record.finish(candidate())
    assert main(['--query','question','--run-id','request1','--output',str(tmp_path),'--text'])==0
    assert capsys.readouterr().out=='yes\nyes\n3'
    assert record.resume()['cached'] and result['completion']=='unverified'


@pytest.mark.parametrize('phase',['prepare_empty_turn','submission_intent','await_current_response'])
def test_restart_interrupted_run_cannot_replay_submission(tmp_path,phase):
    record=RunRecord(tmp_path,'request1','question');record.phase(phase)
    with pytest.raises(RelayFailure,match='no_auto_retry'):RunRecord(tmp_path,'request1','question').resume()


@pytest.mark.parametrize('failure',['wrong_id','wrong_query','unverified','not_fresh','changed_answer','changed_query_restart','existing_fresh'])
def test_request_and_file_attribution_fail_closed(tmp_path,failure):
    record=RunRecord(tmp_path,'request1','question',existing=failure=='existing_fresh');record.phase('preparing')
    raw=candidate()
    if failure=='wrong_id':raw['request_id']='other'
    if failure=='wrong_query':raw['query']='other'
    if failure=='unverified':raw['identity_verified']=False
    if failure=='not_fresh':raw['fresh_submission_verified']=False
    if failure in ('changed_answer','changed_query_restart'):
        record.finish(raw)
        if failure=='changed_answer':(tmp_path/'answer.txt').write_text('unrelated')
        with pytest.raises(RelayFailure):RunRecord(tmp_path,'request1','other' if failure=='changed_query_restart' else 'question').resume()
    else:
        with pytest.raises(RelayFailure):record.finish(raw)
        assert json.loads((tmp_path/'result.json').read_text())['status']=='pending'


def test_missing_pdf_cannot_be_returned_or_reused(tmp_path):
    record=RunRecord(tmp_path,'request1','question');record.phase('awaiting')
    with pytest.raises(RelayFailure,match='pdf_missing'):
        record.finish(dict(kind='document',text='found',pdf=str(tmp_path/'quarantine/missing.pdf'),sha256='wrong',identity_verified=True,fresh_submission_verified=True))


def test_concurrent_request_does_no_work(tmp_path,capsys):
    with open('/tmp/siri-cli-current-turn.lock','a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        assert main(['--query','question','--output',str(tmp_path/'other'),'--diagnostics'])==1
    assert 'another_siri_transaction' in capsys.readouterr().err
    assert not (tmp_path/'other').exists()


def test_private_result_and_symlink_refusal(tmp_path):
    folder=tmp_path/'run';record=RunRecord(folder,'request1','question');record.phase('prepared')
    assert folder.stat().st_mode&0o077==0 and record.path.stat().st_mode&0o077==0
    outside=tmp_path/'outside';outside.write_text('{}');record.path.unlink();record.path.symlink_to(outside)
    with pytest.raises(RelayFailure,match='symlink'):record.resume()


def test_document_expectation_cannot_accept_text_preamble(tmp_path):
 record=RunRecord(tmp_path,'doc-run','diagram',expect='document');record.phase('await_current_response')
 with pytest.raises(RelayFailure,match='expected_kind'):record.finish(candidate())
 assert not (tmp_path/'answer.txt').exists()


def test_keyboard_cancellation_is_not_misattributed_to_user_activity(tmp_path,capsys):
 import sys
 from contextlib import nullcontext
 quartz=N(CGEventSourceSecondsSinceLastEventType=lambda *args:30,
          CGSessionCopyCurrentDictionary=lambda:{'CGSSessionScreenIsLocked':False},
          kCGEventSourceStateCombinedSessionState=0,kCGAnyInputEventType=0)
 with patch.dict(sys.modules,Quartz=quartz,ApplicationServices=N()),patch('siri_cli.local_readback.perform',side_effect=KeyboardInterrupt),patch('siri_cli.capture.deadline.bounded_ax_reads',lambda *args:nullcontext()),patch('siri_cli.local_port.activity_checked_reads',lambda *args:nullcontext()):
  assert main(['--query','synthetic question','--run-id','cancel-run','--output',str(tmp_path),'--diagnostics'])==1
 result=json.loads(capsys.readouterr().err)
 assert result['reason']=='cancelled' and result['user_activity_interruption'] is False
 saved=json.loads((tmp_path/'result.json').read_text())
 assert saved['gui_control_released'] is True and saved['status']=='failed'
 assert not (tmp_path/'answer.txt').exists()


def test_changed_expectation_cannot_reuse_run(tmp_path):
 record=RunRecord(tmp_path,'run','question');record.phase('observing');record.finish(candidate())
 with pytest.raises(RelayFailure,match='expected_kind'):RunRecord(tmp_path,'run','question',expect='document').resume()


def test_completion_claim_is_never_promoted(tmp_path):
 record=RunRecord(tmp_path,'run','question');record.phase('observing');raw=candidate();raw['completion']='verified'
 assert record.finish(raw)['completion']=='unverified'


def test_activity_checked_reads_restore_and_stop(tmp_path):
 from siri_cli.local_port import activity_checked_reads
 calls=[];checks=[]
 def read(*args):calls.append(1);return 0,None
 api=N(AXUIElementCopyAttributeNames=read,AXUIElementCopyAttributeValue=read,AXUIElementCopyActionNames=read)
 def check():
  checks.append(1)
  if len(checks)==2:raise RelayFailure('cancelled')
 with pytest.raises(RelayFailure),activity_checked_reads(check,api):api.AXUIElementCopyAttributeNames(None,None)
 assert len(calls)==1 and api.AXUIElementCopyAttributeNames is read


def test_pid_read_deadline_is_not_mislabeled_as_process_replacement():
 from siri_cli.preview_provider import NativePreviewPort
 from contextlib import nullcontext
 expired=[]
 def pid():expired.append(True);return None
 port=NativePreviewPort(live_enabled=False);port.pid=1;port.backend=N(_find_pid=pid)
 def check():
  if expired:raise RelayFailure('deadline')
 with patch('siri_cli.capture.deadline.bounded_ax_reads',lambda *args:nullcontext()),pytest.raises(RelayFailure,match='deadline'):
  port.observe(N(check=check,deadline=0))


def test_known_source_error_is_explicit_and_does_not_write_answer(tmp_path):
 from siri_cli.response_kind import SiriReportedError
 record=RunRecord(tmp_path,'run','diagram',expect='document');record.phase('await_current_response')
 message='I’m having a bit of trouble here. Please try again.'
 try:
  try:raise SiriReportedError(message)
  except ValueError as e:raise RelayFailure('preview_read_failed') from e
 except RelayFailure as error:result=record.fail(error)
 assert result['status']=='failed' and result['reason']=='siri_reported_error' and result['source_error_message']==message
 assert not (tmp_path/'answer.txt').exists()
