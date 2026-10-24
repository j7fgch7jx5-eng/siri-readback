"""One local request -> attributable rendered text or quarantined PDF. No sender."""
import argparse,fcntl,hashlib,json,os,re,signal,sys,threading,time,uuid
from datetime import datetime,timezone
from pathlib import Path
from .relay import Ticket,Incoming,RelayFailure
from .runtime_policy import FAILURE_MESSAGE,PENDING_MESSAGE,source_error


def source_build_hash(root=None):
    root=Path(root) if root is not None else Path(__file__).resolve().parents[2]
    files={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
           for p in sorted((root/'src').rglob('*.py'))}
    for name in ('readback','review'):
        if (root/name).is_file():files[name]=hashlib.sha256((root/name).read_bytes()).hexdigest()
    return hashlib.sha256(json.dumps(files,sort_keys=True).encode()).hexdigest()


def atomic_json(path,value):
    temp=path.with_name('.'+path.name+'.'+uuid.uuid4().hex)
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'w') as f:json.dump(value,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
        os.replace(temp,path)
    finally:temp.unlink(missing_ok=True)


class RunRecord:
    def __init__(self,directory,run_id,query,existing=False,expect='auto'):
        self.directory=Path(directory);self.directory.mkdir(mode=0o700,parents=True,exist_ok=True)
        info=self.directory.lstat()
        if self.directory.is_symlink() or info.st_uid!=os.getuid() or info.st_mode&0o077:raise RelayFailure('run_directory_not_private')
        self.path=self.directory/'result.json';self.run_id=run_id;self.query=query;self.existing=existing;self.expect=expect
        self.value={'run_id':run_id,'query':query,'mode':'existing' if existing else 'fresh','phase':'created','status':'pending',
                    'submission_attempts':0,'submission_confirmed':False,'completion':'unverified','sent':False,'messages_staged':False,'expect':expect,
                    'started_at':datetime.now(timezone.utc).isoformat()}
    def resume(self):
        if self.path.is_symlink():raise RelayFailure('run_record_is_symlink')
        if not self.path.exists():return None
        previous=json.loads(self.path.read_text())
        if any(previous.get(k)!=self.value[k] for k in ('run_id','query','mode')):raise RelayFailure('run_identity_mismatch')
        if previous.get('expect','auto')!=self.expect:raise RelayFailure('run_expected_kind_mismatch')
        if previous.get('status')!='readable_preview':
            if (previous.get('status') in ('pending','uncertain') or previous.get('response_uncertain') or
                    (previous.get('submission_attempts') and not previous.get('submission_confirmed'))):
                raise RelayFailure('run_already_attempted_no_auto_retry_pending')
            raise RelayFailure('run_already_attempted_no_auto_retry')
        self.validate_files(previous)
        if source_error(previous.get('text')):
            from .response_kind import SiriReportedError
            raise SiriReportedError(previous['text'])
        return dict(previous,cached=True,query_resubmitted=False)
    def phase(self,phase,**values):
        self.value.update(phase=phase,**values);atomic_json(self.path,self.value)
    def validate_files(self,result):
        text=self.directory/'answer.txt'
        if text.is_symlink() or not text.is_file() or hashlib.sha256(text.read_bytes()).hexdigest()!=result.get('text_sha256'):raise RelayFailure('answer_file_changed')
        if text.read_text()!=result.get('text',''):raise RelayFailure('answer_text_changed')
        if result.get('kind')=='document':
            p=Path(result['pdf'])
            if p.is_symlink() or p.parent.resolve()!=self.directory.resolve()/'quarantine' or not p.is_file():raise RelayFailure('pdf_missing_or_outside_quarantine')
            if hashlib.sha256(p.read_bytes()).hexdigest()!=result.get('sha256'):raise RelayFailure('pdf_bytes_changed')
    def finish(self,result):
        if result.get('run_id',self.run_id)!=self.run_id or result.get('request_id',self.run_id)!=self.run_id or result.get('query',self.query)!=self.query:raise RelayFailure('wrong_result_request')
        if result.get('kind') not in ('text','document') or result.get('identity_verified') is not True:raise RelayFailure('unverified_result')
        if self.expect!='auto' and result['kind']!=self.expect:raise RelayFailure('expected_kind_not_received')
        if not self.existing and result.get('fresh_submission_verified') is not True:raise RelayFailure('fresh_provenance_missing')
        if self.existing and result.get('fresh_submission_verified') is True:raise RelayFailure('existing_cannot_be_fresh')
        text=result.get('text','')
        if not isinstance(text,str) or (result['kind']=='text' and not text):raise RelayFailure('readable_text_missing')
        if source_error(text):
            from .response_kind import SiriReportedError
            raise SiriReportedError(text)
        path=self.directory/'answer.txt';fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f:f.write(text)
        result=dict(result,text=text,text_sha256=hashlib.sha256(text.encode()).hexdigest())
        self.validate_files(result)
        self.value.update(result,status='readable_preview',phase='returned',completion='unverified',human_review_required=True,sent=False,messages_staged=False)
        atomic_json(self.path,self.value);return self.value
    def fail(self,error,**details):
        from .capture.ax import ax_read_failure
        from .response_kind import document_bounds_failure
        causes=[];cause=error;seen=set()
        while cause is not None and id(cause) not in seen:
            seen.add(id(cause));causes.append({'type':type(cause).__name__,'reason':str(cause)})
            if hasattr(cause,'source_message'):details.update(source_error_message=cause.source_message)
            if hasattr(cause,'identity_failure'):details.update(identity_failure=cause.identity_failure)
            cause=cause.__cause__
        self.value.update(status='failed',failure_phase=self.value['phase'],reason=str(error),error_type=type(error).__name__,causes=causes,ax_failure=ax_read_failure(error),geometry_failure=document_bounds_failure(error),**details)
        if 'source_error_message' in details:self.value.update(reason='siri_reported_error')
        self.value['failure_domain']=('siri_reported' if 'source_error_message' in details else
            'interruption' if details.get('user_activity_interruption') or str(error)=='cancelled' else
            'deadline' if any('deadline' in c['reason'] for c in causes) else 'helper_guard_or_local_operation')
        if (self.value.get('operation_phase') in ('extract_pdf','read_exposed_pdf') and
                any(c['type']=='PDFExtractionError' for c in causes) and not details.get('user_activity_interruption')):
            self.value['conclusive_failure']=True
        atomic_json(self.path,self.value);return self.value


def perform(ticket,record,validator,emit):
    from .capture import ax
    from .document_response import NativeExistingDocumentUI,read_bound_document,fresh_readable_request
    from .local_port import LocalReadablePort,AXRecovery
    from .response_kind import select_readable
    ui=NativeExistingDocumentUI(live_enabled=True,allow_prior_turn=record.existing,approved_query=ticket.incoming.query)
    ui.region_selector=select_readable;ui.deadline=ticket.deadline;ui.directory=ticket.directory;ui.guard=ticket.check;ui.structure_emit=emit
    try:
        if record.existing:
            record.phase('verify_existing_response')
            backend=ui.backend;pid=backend._find_pid()
            if pid is None:raise RelayFailure('siri_not_running')
            windows=backend._windows(ax.app_for_pid(pid))
            if len(windows)!=1:raise RelayFailure('unique_siri_window_required')
            recovery=AXRecovery(backend,pid,windows[0],emit)
            bound=recovery.call(lambda:ui._source(ticket.incoming.query),ticket.check)
            if record.expect=='document' and bound['region']['kind']=='text-candidate':raise RelayFailure('document_not_exposed_in_existing_response')
            if record.expect=='text' and bound['region']['kind']!='text-candidate':raise RelayFailure('expected_text_received_document')
            if bound['region']['kind']=='text-candidate':
                recovery.call(lambda:ui.verify(bound),ticket.check)
                return {'kind':'text','text':bound['text'],'sources':bound['region'].get('sources',[]),'identity_verified':True,'fresh_submission_verified':False,'existing_response_verified':True,'render_consistency_verified':True}
            record.phase('read_existing_document')
            bound=ui.bind(ticket.incoming.query)
            artifact=read_bound_document(ticket,ui,bound,validator_python=validator)
            return {'kind':'document','text':bound['text'],'pdf':str(artifact.path.resolve()),'bytes':artifact.size,'sha256':artifact.sha256,'source_type':artifact.source_type,'converted':artifact.converted,'identity_verified':True,'fresh_submission_verified':False,'existing_response_verified':True}
        class Port(LocalReadablePort):
            def submit(self,t,before):
                record.phase('submission_intent',submission_attempts=1)
                super().submit(t,before)
                self.response_deadline=min(t.deadline,time.monotonic()+getattr(record,'response_window_s',180))
                record.phase('await_current_response',submission_confirmed=True,response_window_s=getattr(record,'response_window_s',180))
        port=Port(live_enabled=True,emit=emit)
        port.expected_kind=record.expect
        from .startup import ensure_startup
        record.phase('startup')
        ensure_startup(ticket,ui.backend,emit)
        record.phase('prepare_empty_turn')
        result=fresh_readable_request(ticket,port,ui,validator_python=validator)
        result['identity_verified']=True
        return result
    finally:
        record.value['operation_phase']=getattr(ui,'phase','unavailable')
        if getattr(ui,'active_bound',None) is not None:
            try:ui.close_owned_document();record.value['preview_cleanup']='closed'
            except Exception as error:
                record.value['preview_cleanup']='deferred';record.value['cleanup_warning']=str(error)
                emit({'phase':'owned_preview_cleanup_deferred','reason':str(error)})
        else:record.value['preview_cleanup']='not_needed'


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--query',required=True);p.add_argument('--existing',action='store_true',help='read only an exact matching existing turn; never submit')
    p.add_argument('--run-id',default=None);p.add_argument('--output',default=None)
    p.add_argument('--expect',choices=('auto','text','document'),default='auto',help='document waits for an artifact; text never waits for file bytes')
    p.add_argument('--timeout',type=float,default=180,help='response window, 120..600 seconds; default 180, plus bounded startup/artifact margin');p.add_argument('--wait-idle',type=float,default=60)
    p.add_argument('--diagnostics',action='store_true',help='developer-only structured failure output; no repair or retry')
    p.add_argument('--validator-python',default=os.environ.get('SIRI_READBACK_VALIDATOR'));p.add_argument('--text',action='store_true')
    a=p.parse_args(argv)
    if not a.query.strip():p.error('query must be nonempty')
    run_id=a.run_id or uuid.uuid4().hex
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',run_id):p.error('invalid run ID')
    if not (1 if a.existing else 120)<=a.timeout<=600 or not 0<=a.wait_idle<=120:p.error('fresh response window must be 120..600; wait-idle 0..120')
    directory=Path(a.output) if a.output else Path(os.environ.get('SIRI_READBACK_OUTPUT_ROOT',str(Path.cwd()/'readback-runs')))/run_id
    directory=directory.absolute()
    start=time.monotonic();deadline=start+a.timeout+a.wait_idle+60;done=threading.Event();cancel=threading.Event();activity_interrupted=threading.Event();record=None;watcher=None;armed=False;lease=None
    result=None
    try:
        with open('/tmp/siri-cli-current-turn.lock','a+') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise RelayFailure('another_siri_transaction_is_running')
            record=RunRecord(directory,run_id,a.query,a.existing,a.expect)
            result=record.resume()
            if result is None:
                if not a.existing:
                    from .attempt_state import AttemptLease
                    state_root=os.environ.get('SIRI_READBACK_STATE_ROOT',str(Path(__file__).resolve().parents[2]/'local-state'))
                    lease=AttemptLease(state_root);lease.begin(record)
                record.response_window_s=a.timeout
                record.phase('wait_for_idle',build_sha256=source_build_hash())
                import Quartz as Q
                def idle():return Q.CGEventSourceSecondsSinceLastEventType(Q.kCGEventSourceStateCombinedSessionState,Q.kCGAnyInputEventType)
                def unlocked():
                    session=Q.CGSessionCopyCurrentDictionary()
                    return bool(session) and not session.get('CGSSessionScreenIsLocked',False)
                limit=min(deadline,time.monotonic()+a.wait_idle)
                while idle()<20 or not unlocked():
                    if time.monotonic()>=limit:raise RelayFailure('deferred_user_active_or_locked')
                    time.sleep(.1)
                def monitor():
                    previous=idle()
                    while not done.wait(.05):
                        current=idle()
                        if current+.2<previous or not unlocked():activity_interrupted.set();cancel.set();return
                        previous=current
                watcher=threading.Thread(target=monitor,daemon=True);watcher.start()
                def alarm(*unused):raise TimeoutError('local_readback_deadline')
                signal.signal(signal.SIGALRM,alarm);signal.setitimer(signal.ITIMER_REAL,max(.001,deadline-time.monotonic()));armed=True
                ticket=Ticket(run_id,Incoming(run_id,'local-only',a.query),directory,deadline,cancel)
                def emit(event):
                    fd=os.open(directory/'events.jsonl',os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
                    with os.fdopen(fd,'a') as f:f.write(json.dumps(dict(event,elapsed_s=time.monotonic()-start))+'\n')
                from .capture.deadline import bounded_ax_reads
                from .local_port import activity_checked_reads
                import ApplicationServices as A
                with bounded_ax_reads(deadline),activity_checked_reads(ticket.check,A):
                    ticket.check();raw=perform(ticket,record,a.validator_python,emit);ticket.check()
                    result=record.finish(raw)
    except BaseException as error:
        if isinstance(error,(KeyboardInterrupt,SystemExit)):
            cancel.set();original=error;error=RelayFailure('cancelled');error.__cause__=original
        if record is not None and result is None and record.value['phase']!='created':result=record.fail(error,user_activity_interruption=activity_interrupted.is_set())
        else:result={'status':'failed','reason':str(error),'completion':'unverified','sent':False,'messages_staged':False}
    finally:
        if armed:signal.setitimer(signal.ITIMER_REAL,0)
        done.set()
        if watcher:watcher.join(timeout=1)
    if record is not None and lease is not None and lease.owned:
        result.setdefault('run_directory',str(record.directory));lease.finish(result)
    if result.get('reason') in ('previous_request_still_pending','run_already_attempted_no_auto_retry_pending'):result.update(status='pending',response_uncertain=True)
    if result.get('status')!='readable_preview':result['message']=PENDING_MESSAGE if result.get('status')=='pending' else FAILURE_MESSAGE
    result.update(gui_control_released=True,elapsed_s=time.monotonic()-start,finished_at=datetime.now(timezone.utc).isoformat())
    if record is not None and not result.get('cached') and record.value['phase']!='created':atomic_json(record.path,result)
    if a.text and result['status']=='readable_preview' and result['kind']=='text':sys.stdout.write(result['text'])
    elif result['status']=='readable_preview' or a.diagnostics:print(json.dumps(result,ensure_ascii=False,indent=2),file=sys.stdout if result['status']=='readable_preview' else sys.stderr)
    else:print(result['message'],file=sys.stderr)
    return 0 if result['status']=='readable_preview' else 2 if result['status']=='pending' else 1
if __name__=='__main__':raise SystemExit(main())
