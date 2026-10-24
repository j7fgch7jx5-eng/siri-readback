import hashlib,json,threading,time
from types import SimpleNamespace as N
from unittest.mock import patch
import pytest
from siri_cli.local_readback import RunRecord,main
from siri_cli.review_result import main as review
from siri_cli.attempt_state import AttemptLease
from siri_cli.runtime_policy import FAILURE_MESSAGE,PENDING_MESSAGE
from siri_cli.relay import RelayFailure
from siri_cli.capture import ax
from test_turn_scope import fixture


@pytest.mark.parametrize('text',['Sorry, I don’t understand.',"Sorry, I don't understand."])
def test_historical_refusal_review_and_replay_fail_without_editing_journal(tmp_path,capsys,text):
    record=RunRecord(tmp_path,'old','question');record.phase('old-accepted')
    result=record.finish(dict(kind='text',text='valid initially',identity_verified=True,fresh_submission_verified=True))
    result.update(text=text,text_sha256=hashlib.sha256(text.encode()).hexdigest())
    (tmp_path/'answer.txt').write_text(text);(tmp_path/'result.json').write_text(json.dumps(result))
    before={name:(tmp_path/name).read_bytes() for name in ('answer.txt','result.json')}
    with patch('siri_cli.local_readback.perform',side_effect=AssertionError('must not run')):
        assert review([str(tmp_path)])==1
        assert capsys.readouterr().err.strip()==FAILURE_MESSAGE
        assert main(['--query','question','--run-id','old','--output',str(tmp_path)])==1
        assert capsys.readouterr().err.strip()==FAILURE_MESSAGE
    assert before=={name:(tmp_path/name).read_bytes() for name in before}


def test_valid_cache_stays_usable_and_exact(tmp_path,capsys):
    record=RunRecord(tmp_path,'valid','question');record.phase('returned')
    record.finish(dict(kind='text',text='15\n15 ',identity_verified=True,fresh_submission_verified=True))
    before=(tmp_path/'result.json').read_bytes()
    assert review([str(tmp_path),'--json'])==0
    assert json.loads(capsys.readouterr().out)['text']=='15\n15 '
    assert main(['--query','question','--run-id','valid','--output',str(tmp_path),'--text'])==0
    assert capsys.readouterr().out=='15\n15 ' and (tmp_path/'result.json').read_bytes()==before


def test_new_id_and_review_cannot_bypass_pending_attempt(tmp_path,capsys,monkeypatch):
    state=tmp_path/'state';monkeypatch.setenv('SIRI_READBACK_STATE_ROOT',str(state))
    original=RunRecord(tmp_path/'old','old','question');original.phase('await_current_response',submission_attempts=1,submission_confirmed=True)
    lease=AttemptLease(state);lease.begin(original)
    result=original.fail(RelayFailure('response_still_pending'));lease.finish(result)
    (original.directory/'result.json').write_text(json.dumps(result))
    with patch('siri_cli.local_readback.perform',side_effect=AssertionError('no new request')):
        assert main(['--query','new question','--run-id','new','--output',str(tmp_path/'new')])==2
    assert capsys.readouterr().err.strip()==PENDING_MESSAGE
    assert review([str(original.directory)])==2
    assert capsys.readouterr().err.strip()==PENDING_MESSAGE


def test_explicit_failure_ends_then_next_user_request_is_a_separate_attempt(tmp_path,capsys):
    import sys
    from contextlib import nullcontext
    from siri_cli.response_kind import SiriReportedError
    q=N(CGEventSourceSecondsSinceLastEventType=lambda *args:30,
        CGSessionCopyCurrentDictionary=lambda:{'CGSSessionScreenIsLocked':False},
        kCGEventSourceStateCombinedSessionState=0,kCGAnyInputEventType=0)
    calls=[]
    def perform(ticket,record,validator,emit):
        calls.append(ticket.request_id)
        record.phase('await_current_response',submission_attempts=1,submission_confirmed=True)
        if ticket.request_id=='first':raise SiriReportedError('Sorry, I don’t understand.')
        return dict(kind='text',text='A readable answer.',identity_verified=True,fresh_submission_verified=True)
    with patch.dict(sys.modules,Quartz=q,ApplicationServices=N()),patch('siri_cli.local_readback.perform',side_effect=perform),patch('siri_cli.capture.deadline.bounded_ax_reads',lambda *args:nullcontext()),patch('siri_cli.local_port.activity_checked_reads',lambda *args:nullcontext()):
        assert main(['--query','same prompt','--run-id','first','--output',str(tmp_path/'first')])==1
        output=capsys.readouterr();assert output.out=='' and output.err.strip()==FAILURE_MESSAGE
        assert calls==['first']
        assert main(['--query','same prompt','--run-id','second','--output',str(tmp_path/'second')])==0
        assert json.loads(capsys.readouterr().out)['text']=='A readable answer.'
    assert calls==['first','second']
    assert json.loads((tmp_path/'first/result.json').read_text())['status']=='failed'
    assert json.loads((tmp_path/'second/result.json').read_text())['submission_attempts']==1


@pytest.mark.parametrize('outcome',['pending','siri_error','artifact_error','success','before_submit'])
def test_persistent_pending_lease_survives_restart_and_only_conclusive_outcomes_release(tmp_path,outcome):
    state=tmp_path/'state';record=RunRecord(tmp_path/'run','one','question')
    lease=AttemptLease(state);lease.begin(record)
    r=dict(run_id='one',status='failed',submission_attempts=1,submission_confirmed=True,reason='response_still_pending')
    if outcome=='siri_error':r['reason']='siri_reported_error'
    if outcome=='artifact_error':r['conclusive_failure']=True
    if outcome=='success':r['status']='readable_preview'
    if outcome=='before_submit':r['submission_attempts']=0
    lease.finish(r)
    next_record=RunRecord(tmp_path/'next','two','question')
    if outcome=='pending':
        assert r['status']=='pending' and r['message']==PENDING_MESSAGE
        with pytest.raises(RelayFailure,match='previous_request_still_pending'):AttemptLease(state).begin(next_record)
    else:AttemptLease(state).begin(next_record)


@pytest.mark.parametrize('mode',['running','cold','orb','timeout','activity','launch_error'])
def test_startup_has_one_normal_open_and_no_query_or_menu(mode):
    from siri_cli.startup import ensure_startup
    root,c,*_=fixture(prompt=None,answer=None);window=object();calls=[];clock=[0]
    state={'pid':None if mode in ('cold','launch_error') else 1,'ready':mode=='running'}
    def launch():
        calls.append('launch')
        if mode=='launch_error':raise RelayFailure('startup_launch_failed')
        state['pid']=1;state['ready']=mode!='timeout'
    def menu(app):raise AssertionError('startup must not run menu repair actions')
    def guard():
        if mode=='activity':raise RelayFailure('cancelled')
    backend=N(_find_pid=lambda:state['pid'],_windows=lambda app:[window] if state['ready'] else [],_open_new_conversation_menu=menu)
    ticket=N(deadline=100,check=guard)
    with patch.object(ax,'app_for_pid',return_value=None),patch.object(ax,'snapshot_tree',return_value=root):
        if mode in ('running','cold','orb'):assert ensure_startup(ticket,backend,launch=launch,clock=lambda:clock[0],pause=lambda s:clock.__setitem__(0,clock[0]+1))==1
        else:
            with pytest.raises(RelayFailure):ensure_startup(ticket,backend,launch=launch,clock=lambda:clock[0],pause=lambda s:clock.__setitem__(0,clock[0]+1))
    assert calls.count('launch')<=1 and 'menu' not in calls and 'activate' not in calls


@pytest.mark.parametrize('mode',['slow_success','still_pending','explicit_error'])
def test_response_wait_does_not_confuse_two_minute_progress_with_error(mode):
    from siri_cli.document_response import submit_and_wait
    from siri_cli.preview_provider import Turn
    from siri_cli.response_kind import SiriReportedError
    refs=tuple(object() for _ in range(6));identity=(1,*refs[:4],None,None);clock=[0];calls=[]
    class Port:
        response_deadline=180
        def prepare(self,t):return Turn(identity,'',[],0,raw_children=0)
        def submit(self,t,before):calls.append('submit')
        def observe(self,t):
            if mode=='explicit_error':raise SiriReportedError('Sorry, I don’t understand.')
            ready=mode=='slow_success' and clock[0]>=140
            return Turn((1,*refs[:4],refs[4],refs[5]),'',['question'],1,ready=ready)
        def pause(self,t):clock[0]+=70
    ticket=N(request_id='run',incoming=N(query='question'),deadline=300,check=lambda:None)
    with patch('siri_cli.document_response.time.monotonic',lambda:clock[0]),patch('siri_cli.capture.transaction._same_ref',lambda a,b:a is b):
        if mode=='slow_success':
            state,turn=submit_and_wait(ticket,Port(),N());assert turn.ready and clock[0]==140
        else:
            with pytest.raises((RelayFailure,SiriReportedError)):submit_and_wait(ticket,Port(),N())
    assert calls==['submit']
