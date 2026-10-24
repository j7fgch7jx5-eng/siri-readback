"""Explicit local existing-response readback. No ask(), listener or send API."""
import dataclasses,fcntl,hashlib,json,time
from pathlib import Path
from .relay import RelayFailure
from .readable_document import extract_readable_after_copy
from .pdf_extract import MacPasteboard

DRAWING_QUERY='Find the sample document.'


def existing_document_request(ticket,ui,*,validator_python=None,image_python=None):
    """Caller owns the UI lease. Existing-turn evidence never becomes fresh proof."""
    ui.phase='bind';ticket.check();bound=ui.bind(ticket.incoming.query)
    artifact=read_bound_document(ticket,ui,bound,validator_python=validator_python,image_python=image_python)
    text=bound['text']
    draft={'request_id':ticket.request_id,'query':ticket.incoming.query,'recipient':'local-review',
           'text':text,'attachment':str(artifact.path.resolve()),'attachment_sha256':artifact.sha256,
           'status':'draft_only','sent':False,'completion':'unverified','fresh_submission_verified':False,
           'source':'owner_approved_existing_response','human_review_required':True}
    with open(ticket.directory/'reply-draft.json','w') as stream:json.dump(draft,stream,ensure_ascii=False,indent=2)
    return artifact,draft


def read_bound_document(ticket,ui,bound,*,validator_python=None,image_python=None):
    if validator_python is None:
        from .pdf_extract import validator_interpreter
        validator_python=validator_interpreter()
    urls=bound.get('payload_urls',[]) if bound.get('region',{}).get('kind')=='pdf-document' else []
    if len(urls)>1:raise RelayFailure('native_payload_urls_ambiguous')
    if urls:
        from .pdf_extract import _file_bytes,MAX_BYTES,validate_pdf,_quarantine
        from .readable_document import ReadableDocument
        ui.phase='read_exposed_pdf';ui.verify(bound);ticket.check()
        data=_file_bytes(urls[0].encode('utf-8'),MAX_BYTES)
        validate_pdf(data,python=validator_python,timeout=max(.001,min(5,ticket.deadline-time.monotonic())))
        ui.verify(bound);ticket.check()
        path=_quarantine(ticket.directory/'quarantine',data)
        try:ui.verify(bound);ticket.check()
        except BaseException:path.unlink(missing_ok=True);raise
        return ReadableDocument(path,len(data),hashlib.sha256(data).hexdigest(),'public.file-url',False)
    ui.phase='open_document';ticket.check();ui.open_document(bound)
    ui.phase='open_share';ticket.check();ui.open_share(bound)
    before=ui.pasteboard.change_count()
    ui.phase='copy_document';ticket.check();ui.verify(bound);ui.copy_document(bound)
    until=min(ticket.deadline,time.monotonic()+4)
    while ui.pasteboard.change_count()==before and time.monotonic()<until:
        ticket.check();time.sleep(.02)
    ui.verify(bound);ticket.check()
    ui.phase='extract_pdf'
    artifact=extract_readable_after_copy(ui.pasteboard,before,ticket.directory/'quarantine',
       validator_python=validator_python,image_python=image_python,deadline=ticket.deadline,
       log=ui.audit)
    try:ui.verify(bound);ticket.check()
    except BaseException:
        artifact.path.unlink(missing_ok=True)
        raise
    return artifact


class NativeExistingDocumentUI:
    def __init__(self,*,live_enabled=False,allow_prior_turn=False,approved_query=DRAWING_QUERY):
        if live_enabled is not True:raise RelayFailure('document_ui_disabled')
        from .driver.siriai import SiriAiBackend
        self.backend=SiriAiBackend();self.pasteboard=MacPasteboard();self.deadline=None
        self.copy_attempted=False;self.open_attempted=False
        self.allow_prior_turn=allow_prior_turn;self.approved_query=approved_query
    def audit(self,event):
        if hasattr(self,'directory'):
            with open(self.directory/'document-types.jsonl','a') as f:f.write(json.dumps(event)+'\n')
    def _check(self):
        callback=getattr(self,'guard',None)
        if callback:callback()
    def _source(self,query,old=None):
        self._check()
        from .capture import ax
        from .capture.transaction import _same_ref
        from .response_kind import select_document
        pid=self.backend._find_pid()
        self._check()
        if pid is None:raise RelayFailure('siri_not_running')
        if old and pid!=old['pid']:raise RelayFailure('source_process_replaced')
        windows=self.backend._windows(ax.app_for_pid(pid))
        if old:windows=[w for w in windows if _same_ref(w,old['window'])]
        if len(windows)!=1:raise RelayFailure('source_window_ambiguous')
        root=ax.snapshot_tree(windows[0],structural=True)
        if not root.complete:raise RelayFailure('source_incomplete')
        c=ax.find_composer_node(root)
        if c is None:raise RelayFailure('source_composer_unavailable')
        pane=ax.conversation_container(root,c);transcript=ax.transcript_container(pane)
        ps=[n for n in ax.nodes(transcript) if n.element.identifier=='userPrompt'];rs=[n for n in ax.nodes(transcript) if n.element.identifier=='modelResponse']
        # Reject replacement handles before reading any replacement response.
        expected=getattr(self,'expected_binding',None)
        if expected is not None:
            current=(pid,windows[0],pane.ref,c.ref,transcript.ref,
                     ps[0].ref if len(ps)==1 else None,rs[0].ref if len(rs)==1 else None)
            if current[0]!=expected[0] or not all(_same_ref(a,b) for a,b in zip(current[1:],expected[1:])):
                raise RelayFailure('fresh_document_identity_mismatch')
        if old and 'refs' in old:
            outer=(windows[0],pane.ref,transcript.ref,c.ref)
            if (not all(_same_ref(a,b) for a,b in zip(outer,old['refs'][:4])) or
                    not any(_same_ref(n.ref,old['refs'][4]) for n in ps) or
                    not any(_same_ref(n.ref,old['refs'][5]) for n in rs)):
                raise RelayFailure('source_changed')
        if getattr(self,'allow_prior_turn',False):
            events=[n for n in ax.nodes(transcript) if n.element.identifier in ('userPrompt','modelResponse')]
            if len(events)>8:raise RelayFailure('prior_turn_scope_too_large')
            matches=[i for i,n in enumerate(events) if n.element.identifier=='userPrompt' and '\n'.join(ax.extract_scoped_texts(ax.snapshot_tree(n.ref)))==query]
            if len(matches)!=1 or matches[0]+1>=len(events) or events[matches[0]+1].element.identifier!='modelResponse':raise RelayFailure('prior_turn_unverified')
            ps=[events[matches[0]]];rs=[events[matches[0]+1]]
        if len(ps)!=1 or len(rs)!=1:raise RelayFailure('source_turn_ambiguous')
        prompt='\n'.join(ax.extract_scoped_texts(ax.snapshot_tree(ps[0].ref)))
        if prompt!=query or ax._get_attr_checked(c.ref,'AXValue'):raise RelayFailure('source_prompt_or_composer_changed')
        response=ax.snapshot_tree(rs[0].ref)
        if getattr(self,'structure_emit',None):
            from .response_kind import response_structure
            self.structure_emit({'phase':'scoped_response_structure','structure':response_structure(response)})
        selector=getattr(self,'region_selector',select_document)
        try:region=selector(response,ax._element_bounds(transcript.ref,reader=ax._get_attr_checked))
        except ValueError as error:
            # An explicitly approved prior turn may be above the viewport.
            # One semantic scroll, before binding; never relax containment.
            if str(error)!='document_bounds_rejected' or not getattr(self,'allow_prior_turn',False) or getattr(self,'prior_scroll_attempted',False) or old is not None:raise
            cards=[n for n in ax.nodes(response) if (n.element.identifier or '').startswith('Identifier:ResultCell')]
            if len(cards)!=1:raise
            # The inner list can move content without revealing this response
            # in its outer transcript. Only scroll a verified, fitting response.
            geometry=getattr(error,'geometry',None)
            if (not geometry or geometry['failed_containment']!=['transcript'] or
                    'AXScrollToVisible' not in response.element.actions):raise
            import ApplicationServices as A
            self._check();self.prior_scroll_attempted=True
            if A.AXUIElementPerformAction(response.ref,'AXScrollToVisible'):raise RelayFailure('prior_document_scroll_uncertain')
            return self._source(query,{'pid':pid,'window':windows[0]})
        cells=[n for n in ax.nodes(response) if (n.element.identifier or '').startswith('Identifier:ResultCell')]
        if region['kind']=='text-candidate':container=target=response
        else:container,target=(cells[0],cells[0]) if cells else (response.children[1],response.children[1].children[0])
        refs=(windows[0],pane.ref,transcript.ref,c.ref,ps[0].ref,rs[0].ref,container.ref,target.ref)
        signature=hashlib.sha256(json.dumps([dataclasses.asdict(n.element) for n in ax.nodes(response)],sort_keys=True).encode()).hexdigest()
        text='\n'.join(n.element.text for n in response.children if n.element.identifier=='modelResponse-richTextParagraph' and n.element.text)
        text=region.get('text',text)
        payload_urls=sorted({str(v) for n in ax.nodes(response) for v in (n.element.url,n.element.document) if v and str(v).startswith('file:')})
        return {'pid':pid,'window':windows[0],'refs':refs,'signature':signature,'text':text,'query':query,'region':region,'payload_urls':payload_urls}
    def bind(self,query):
        if query!=getattr(self,'approved_query',DRAWING_QUERY):raise RelayFailure('unapproved_document_query')
        result=self._source(query)
        import AppKit as K
        app=K.NSRunningApplication.runningApplicationWithProcessIdentifier_(result['pid'])
        if int(K.NSWorkspace.sharedWorkspace().frontmostApplication().processIdentifier())!=result['pid']:
            app.activateWithOptions_(K.NSApplicationActivateIgnoringOtherApps)
        until=min(self.deadline,time.monotonic()+3)
        while int(K.NSWorkspace.sharedWorkspace().frontmostApplication().processIdentifier())!=result['pid']:
            if time.monotonic()>=until:raise RelayFailure('siri_foreground_unverified')
            # Deliver Cocoa activation/workspace notifications while bounded;
            # sleeping alone can leave the command-line process's view stale.
            self._check()
            import Foundation as F
            F.NSRunLoop.currentRunLoop().runUntilDate_(F.NSDate.dateWithTimeIntervalSinceNow_(.03))
        self.verify(result);return result
    def verify(self,bound):
        from .capture.transaction import _same_ref
        now=self._source(bound['query'],bound)
        if (now['signature']!=bound['signature'] or now['region']!=bound['region'] or
            not all(_same_ref(a,b) for a,b in zip(bound['refs'],now['refs']))):raise RelayFailure('source_changed')
        if 'document' in bound:
            from .capture import ax
            windows=self.backend._windows(ax.app_for_pid(bound['pid']))
            if not any(_same_ref(w,bound['document']) for w in windows):raise RelayFailure('document_replaced')
    def _wait_share(self,bound):
        from .capture import ax
        from .capture.transaction import _same_ref
        until=min(self.deadline,time.monotonic()+5)
        while time.monotonic()<until:
            self._check();found=[]
            for w in self.backend._windows(ax.app_for_pid(bound['pid'])):
                if _same_ref(w,bound['window']):continue
                root=ax.snapshot_tree(w,structural=True)
                shares=[n for n in ax.nodes(root) if n.element.identifier=='QLControlShare']
                if root.complete and len(shares)==1:found.append((w,shares[0]))
            if len(found)==1:return found[0]
            if len(found)>1:raise RelayFailure('document_preview_ambiguous')
            time.sleep(.03)
        raise RelayFailure('document_preview_not_observed')
    def open_document(self,bound):
        if self.open_attempted:raise RelayFailure('document_open_retry_forbidden')
        from .capture import ax
        from .capture.transaction import _same_ref
        import ApplicationServices as A
        existing=self.backend._windows(ax.app_for_pid(bound['pid']))
        if len(existing)!=1 or not _same_ref(existing[0],bound['window']):raise RelayFailure('unbound_document_preview_exists')
        self.verify(bound);self.open_attempted=True
        self._check()
        if bound['region']['kind']=='pdf-document':
            enabled=ax._get_attr_checked(bound['refs'][-1],'AXEnabled')
            if enabled is None:raise RelayFailure('native_document_enabled_unverified')
            if not bool(enabled):raise RelayFailure('native_document_disabled')
        if A.AXUIElementPerformAction(bound['refs'][-1],'AXPress'):raise RelayFailure('document_open_uncertain')
        doc,share=self._wait_share(bound);bound['document']=doc;bound['share']=share.ref
        self.active_bound=bound
        self.verify(bound)
    def close_owned_document(self):
        """Close only this run's retained Quick Look reference; never other apps."""
        bound=getattr(self,'active_bound',None)
        if bound is None:return
        from .capture import ax
        from .capture.transaction import _same_ref
        import ApplicationServices as A
        self._check();self._cleanup_read(lambda:self.verify(bound),bound)
        native_close=ax._get_attr_checked(bound['document'],'AXCloseButton')
        if native_close is not None:
            if ax._get_attr_checked(native_close,'AXSubrole')!='AXCloseButton':raise RelayFailure('owned_preview_close_unverified')
            close_ref=native_close
        else:
            root=ax.snapshot_tree(bound['document'],structural=True)
            close=[n for n in ax.nodes(root) if n.element.role=='AXButton' and
                   (n.element.identifier=='QLControlClose' or ax._get_attr_checked(n.ref,'AXDescription')=='Close')]
            if not root.complete or len(close)!=1:raise RelayFailure('owned_preview_close_unverified')
            close_ref=close[0].ref
        self._check()
        if A.AXUIElementPerformAction(close_ref,'AXPress'):raise RelayFailure('owned_preview_close_uncertain')
        until=min(self.deadline,time.monotonic()+2)
        while time.monotonic()<until:
            self._check()
            windows=self._cleanup_read(lambda:self.backend._windows(ax.app_for_pid(bound['pid'])),bound)
            if not any(_same_ref(w,bound['document']) for w in windows):
                del bound['document']
                self._cleanup_read(lambda:self.verify(bound),bound)
                self.active_bound=None;return
            time.sleep(.02)
        raise RelayFailure('owned_preview_not_closed')
    def _cleanup_read(self,read,bound):
        """One full read reacquisition; never replays a close/open/Copy action."""
        from .capture import ax
        from .driver.base import CaptureError
        import re
        try:return read()
        except Exception as error:
            failure=ax.ax_read_failure(error)
            code=failure['error_code'] if failure else None
            if isinstance(error,CaptureError):
                match=re.fullmatch(r'AX application (?:children|child role) read failed \((-?\d+)\)',str(error))
                if match:code=int(match.group(1))
            if code not in (-25202,-25200,-25204) or getattr(self,'cleanup_reacquired',False):raise
            self._check();pid=self.backend._find_pid();self._check()
            if pid!=bound['pid']:raise RelayFailure('cleanup_process_replaced') from error
            self.cleanup_reacquired=True
            if getattr(self,'structure_emit',None):self.structure_emit({'phase':'cleanup_read_reacquisition','error_code':code,'action_repeated':False})
            return read()
    def open_share(self,bound):
        import ApplicationServices as A
        from .capture import ax
        self.verify(bound)
        self._check()
        if A.AXUIElementPerformAction(bound['share'],'AXPress'):raise RelayFailure('document_share_uncertain')
        until=min(self.deadline,time.monotonic()+3)
        while time.monotonic()<until:
            self._check();root=ax.snapshot_tree(bound['document'],structural=True);copies=[]
            for pop in ax.nodes(root):
                if pop.element.role!='AXPopover' or pop.element.identifier!='ShareSheet.Popover':continue
                for node in ax.nodes(pop):
                    if node.element.role=='AXButton' and 'AXPress' in node.element.actions and any(ax._get_attr_checked(node.ref,k)=='Copy' for k in ('AXTitle','AXDescription')):copies.append(node)
            if root.complete and len(copies)==1:
                enabled=ax._get_attr_checked(copies[0].ref,'AXEnabled')
                if enabled is None or not bool(enabled):raise RelayFailure('document_copy_disabled')
                bound['copy']=copies[0].ref;return
            if len(copies)>1:raise RelayFailure('document_copy_ambiguous')
            time.sleep(.03)
        raise RelayFailure('document_copy_unavailable')
    def copy_document(self,bound):
        import ApplicationServices as A
        if self.copy_attempted:raise RelayFailure('copy_retry_forbidden')
        self.verify(bound);self.copy_attempted=True
        self._check()
        if A.AXUIElementPerformAction(bound['copy'],'AXPress'):raise RelayFailure('document_copy_uncertain')


def fresh_document_request(ticket,turn_port,ui,*,validator_python=None,image_python=None):
    """One submission; the original pre-submission binding must reach the PDF."""
    from .capture.transaction import SubmissionState,_same_ref
    from .preview_provider import apply_placeholder_handoff,observe_preview_turn
    state,turn=submit_and_wait(ticket,turn_port,ui)
    ui.phase='bind_fresh_document';bound=ui.bind(ticket.incoming.query)
    # Native document refs: window,pane,transcript,composer,prompt,response,...
    refs=bound['refs'];mapped=(bound['pid'],refs[0],refs[1],refs[3],refs[2],refs[4],refs[5])
    if mapped[0]!=state.binding[0] or not all(_same_ref(a,b) for a,b in zip(mapped[1:],state.binding[1:])):
        raise RelayFailure('fresh_document_identity_mismatch')
    artifact=read_bound_document(ticket,ui,bound,validator_python=validator_python,image_python=image_python)
    artifact=dataclasses.replace(artifact,fresh_submission_verified=True)
    return artifact,{'request_id':ticket.request_id,'query':ticket.incoming.query,
       'text':bound['text'],'pdf':str(artifact.path.resolve()),'sha256':artifact.sha256,
       'fresh_submission_verified':True,'completion':'unverified','ocr_invoked':False,
       'sent':False,'messages_staged':False}


def submit_and_wait(ticket,turn_port,ui):
    from .capture.transaction import SubmissionState
    from .preview_provider import apply_placeholder_handoff,observe_preview_turn
    if getattr(ui,'allow_prior_turn',False):raise RelayFailure('prior_turn_mode_forbidden_for_fresh_request')
    ui.phase='prepare_fresh';ticket.check();before=turn_port.prepare(ticket)
    if before.raw_children or before.prompts or before.response_count or before.composer:
        raise RelayFailure('fresh_empty_turn_required')
    state=SubmissionState(ticket.request_id,ticket.incoming.query,binding=before.identity)
    ui.phase='submit_once';ticket.check()
    try:turn_port.submit(ticket,before)
    except Exception as error:raise RelayFailure('submission_uncertain') from error
    ui.phase='await_current_document'
    while True:
        ticket.check()
        if time.monotonic()>=getattr(turn_port,'response_deadline',ticket.deadline):raise RelayFailure('response_still_pending')
        turn=turn_port.observe(ticket)
        if turn.handoff is not None:apply_placeholder_handoff(state,turn)
        try:observe_preview_turn(state,turn)
        except ValueError as error:raise RelayFailure('fresh_turn_interrupted') from error
        if state.acknowledged and turn.ready:break
        turn_port.pause(ticket)
    return state,turn


def fresh_readable_request(ticket,turn_port,ui,**options):
    state,turn=submit_and_wait(ticket,turn_port,ui)
    kind=turn_port.snapshot[0]['kind']
    if kind=='text-candidate':
        from .response_kind import read_bound_text
        ui.phase='verify_rendered_text'
        reader=getattr(turn_port,'read_text',read_bound_text)
        text=reader(ticket,turn_port,state.binding)
        return {'status':'fresh_rendered_text_candidate','kind':'text','text':text,
          'sources':getattr(turn_port,'text_sources',[]),
          'request_id':ticket.request_id,'fresh_submission_verified':True,
          'render_consistency_verified':True,'completion':'unverified',
          'strict_text_completion_verified':False,'human_review_required':True,
          'sent':False,'messages_staged':False,'ocr_invoked':False}
    from .capture.transaction import _same_ref
    ui.expected_binding=state.binding
    ui.phase='bind_fresh_document';bound=ui.bind(ticket.incoming.query)
    refs=bound['refs'];mapped=(bound['pid'],refs[0],refs[1],refs[3],refs[2],refs[4],refs[5])
    if mapped[0]!=state.binding[0] or not all(_same_ref(a,b) for a,b in zip(mapped[1:],state.binding[1:])):raise RelayFailure('fresh_document_identity_mismatch')
    artifact=read_bound_document(ticket,ui,bound,**options)
    return {'status':'fresh_pdf_verified','kind':'document','pdf':str(artifact.path.resolve()),
       'sha256':artifact.sha256,'bytes':artifact.size,'source_type':artifact.source_type,'converted':artifact.converted,'text':bound['text'],'request_id':ticket.request_id,
       'fresh_submission_verified':True,'completion':'unverified','sent':False,
       'messages_staged':False,'ocr_invoked':False,'human_review_required':True}
