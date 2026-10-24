"""Separate request-bound preview provider; strict ask() is never called."""
from dataclasses import dataclass
import hashlib
import json
import dataclasses
import time
from .relay import Preview,RelayFailure,validate_png
from .capture.transaction import SubmissionState,_same_ref


@dataclass
class Turn:
    identity: tuple
    composer: str
    prompts: list
    response_count: int
    raw_children: int = 0
    ready: bool = False
    fingerprint: str | None = None
    handoff: dict | None = None


class SiriPreviewProvider:
    def __init__(self,port):self.port=port

    def __call__(self,ticket):
        ticket.check();before=self.port.prepare(ticket)
        if before.raw_children or before.prompts or before.response_count or before.composer:
            raise RelayFailure('fresh_empty_turn_required')
        state=SubmissionState(ticket.request_id,ticket.incoming.query,binding=before.identity)
        ticket.check()
        # No retry, including partial writes, AX timeout, or uncertain confirm.
        try:self.port.submit(ticket,before)
        except Exception as exc:raise RelayFailure('submission_uncertain') from exc
        while True:
            ticket.check();turn=self.port.observe(ticket)
            if turn.handoff is not None:
                apply_placeholder_handoff(state,turn)
            try:observe_preview_turn(state,turn)
            except ValueError as exc:raise RelayFailure('turn_interrupted') from exc
            if state.acknowledged and turn.ready:
                result=self.port.capture(ticket,turn)
                ticket.check();after=self.port.observe(ticket)
                try:state.observe(after.composer,after.prompts,after.response_count,identity=after.identity)
                except ValueError as exc:raise RelayFailure('turn_interrupted') from exc
                if not after.ready or after.fingerprint!=turn.fingerprint:raise RelayFailure('preview_changed')
                if not isinstance(result,Preview) or result.ticket is not ticket:
                    raise RelayFailure('wrong_request')
                return result
            self.port.pause(ticket)


def observe_preview_turn(state,turn):
    # Loading modelResponse nodes are not answer identities. Pin the first
    # positive preview only; retain exact outer/prompt provenance meanwhile.
    if len(turn.prompts)>1 or turn.response_count>1:raise ValueError('multiple turns after submission')
    if state.binding is not None and state.binding[6] is None and not turn.ready:
        if bool(turn.identity[6] is not None)!=bool(turn.response_count):raise ValueError('response structure disagrees')
        return state.observe(turn.composer,turn.prompts,0,identity=(*turn.identity[:6],None))
    return state.observe(turn.composer,turn.prompts,turn.response_count,identity=turn.identity)


def allowed_placeholder_transition(before,after):
    """Only the observed empty unknown -> scroll-area allocation, same slot."""
    try:
        old,new=before['nodes']['transcript'],after['nodes']['transcript']
        return (before['phase']=='before_submit' and before['raw_children']==0 and
            before['prompt_count']==before['response_count']==0 and
            old['role']=='AXUnknown' and new['role']=='AXScrollArea' and
            old['identifier']==new['identifier']=='chatSessionView' and
            old['path']==new['path'] and old['path'] is not None and
            len(old['path'])>len(before['nodes']['pane']['path']) and
            old['path'][:len(before['nodes']['pane']['path'])]==before['nodes']['pane']['path'] and
            before['nodes']['pane']['path']==after['nodes']['pane']['path'] and
            after['phase']=='after_submit' and
            after['same']=={'pid':True,'window':True,'pane':True,'composer':True,'transcript':False} and
            after['raw_children'] in (0,1) and after['prompt_count'] in (0,1) and after['response_count'] in (0,1))
    except (KeyError,TypeError):return False


def apply_placeholder_handoff(state,turn):
    evidence=turn.handoff
    old=evidence['old_identity'];new=turn.identity
    if (state.acknowledged or getattr(state,'preview_handoff_used',False) or
        state.binding is None or state.binding[5:]!=(None,None) or
        any(not _same_ref(a,b) for a,b in zip(state.binding,old)) or
        new[0]!=old[0] or any(not _same_ref(a,b) for a,b in zip(old[1:4],new[1:4])) or
        _same_ref(old[4],new[4]) or not allowed_placeholder_transition(evidence['before'],evidence['after']) or
        turn.composer not in ('',state.query)):
        raise RelayFailure('unverified_transcript_handoff')
    # Keep all outer references and run provenance; bind actual allocated
    # transcript once, before any prompt/response identity or answer is read.
    state.binding=(*state.binding[:4],new[4],None,None)
    state.preview_handoff_used=True


def scope_changes(before,after):
    """Named identity comparisons only; never include text or raw AX handles."""
    names=('pid','window','pane','composer','transcript')
    if len(before)<5 or len(after)<5:raise RelayFailure('scope_identity_incomplete')
    return {name:(a==b if i==0 else _same_ref(a,b)) for i,(name,a,b) in enumerate(zip(names,before[:5],after[:5]))}


class NativePreviewPort:
    """Production guard is reused. Live operations default disabled.

    Caller holds the relay execution lease. Default Vision reviewer examines
    THIS image; no cached approval is accepted.
    """
    def __init__(self,reviewer=None,*,live_enabled=False,region_selector=None):
        from .pixel_review import PixelReviewer
        self.reviewer=reviewer or PixelReviewer();self.live_enabled=live_enabled;self.backend=None
        self.region_selector=region_selector

    def snapshot_target(self,ticket):
        from .capture import current
        return (current.snapshot_target(self.backend,self.pid,ticket.incoming.query,
                    region_selector=self.region_selector) if self.region_selector else
                current.snapshot_target(self.backend,self.pid,ticket.incoming.query))

    def prepare(self,ticket):
        if not self.live_enabled:raise RelayFailure('live_siri_disabled')
        if self.reviewer is None:raise RelayFailure('image_reviewer_not_connected')
        from .driver.siriai import SiriAiBackend
        self.backend=SiriAiBackend();self.pid=self.backend._find_pid()
        if hasattr(self,'base'):del self.base
        if hasattr(self,'scope_baseline'):del self.scope_baseline
        self.submitted=False
        if self.pid is None:raise RelayFailure('siri_not_running')
        prepared=self.prepare_empty(ticket)
        self.scope_baseline=self.scope_observation
        self.handoff_used=False
        return prepared

    def prepare_empty(self,ticket):
        before=self.observe(ticket)
        if before.composer:raise RelayFailure('composer_in_use')
        if not before.raw_children and not before.response_count:return before
        # One exact menu command preserves existing conversations. Never select
        # sidebar history or clear the existing transcript. No action retry.
        from .capture import ax
        from .capture.deadline import bounded_ax_reads
        check=self.observe(ticket)
        if check.composer or any(not _same_ref(a,b) for a,b in zip(check.identity,before.identity)) or check.raw_children!=before.raw_children:
            raise RelayFailure('preparation_interrupted')
        with bounded_ax_reads(ticket.deadline):
            if not self.backend._open_new_conversation_menu(ax.app_for_pid(self.pid)):
                raise RelayFailure('new_conversation_unavailable')
        old=self.base
        del self.base
        while True:
            ticket.check()
            # New-conversation replacement is allowed only during preparation,
            # before binding or submitting. The PID and window must stay fixed.
            if hasattr(self,'base'):del self.base
            current=self.observe(ticket)
            if current.identity[0]!=old[0] or not _same_ref(current.identity[1],old[1]):
                raise RelayFailure('preparation_window_replaced')
            if current.composer:raise RelayFailure('composer_in_use')
            if not current.raw_children and not current.response_count:
                return current
            if any(not _same_ref(a,b) for a,b in zip(current.identity[2:5],old[2:5])):
                raise RelayFailure('new_pane_not_empty')
            self.pause(ticket)

    def observe(self,ticket):
        from .capture import ax,current
        from .capture.deadline import bounded_ax_reads
        ticket.check()
        with bounded_ax_reads(ticket.deadline):
            if self.backend._find_pid()!=self.pid:raise RelayFailure('siri_process_replaced')
            windows=self.backend._windows(ax.app_for_pid(self.pid))
            if len(windows)!=1:raise RelayFailure('unique_siri_window_required')
            root=ax.snapshot_tree(windows[0],structural=True)
            composer=ax.find_composer_node(root)
            if composer is None:raise RelayFailure('unique_composer_required')
            pane=ax.conversation_container(root,composer)
            if pane is None:raise RelayFailure('unique_pane_required')
            transcript=ax.transcript_container(pane)
            prompts=[n for n in ax.nodes(transcript) if n.element.identifier=='userPrompt']
            responses=[n for n in ax.nodes(transcript) if n.element.identifier=='modelResponse']
            identity=(self.pid,windows[0],pane.ref,composer.ref,transcript.ref,
                prompts[0].ref if len(prompts)==1 else None,responses[0].ref if len(responses)==1 else None)
            # Record structural evidence before rejecting or reading contents.
            def descriptor(node):
                path=ax._path_to(root,node)
                indices=[next(i for i,candidate in enumerate(parent.children) if candidate is child) for parent,child in zip(path,path[1:])] if path else None
                return {'role':node.element.role,'identifier':node.element.identifier,
                        'bounds':node.element.bounds,'path':indices}
            self.scope_observation={'phase':'after_submit' if self.submitted else 'before_submit',
                'same':scope_changes(self.base,identity) if hasattr(self,'base') else None,
                'nodes':{'pane':descriptor(pane),'composer':descriptor(composer),'transcript':descriptor(transcript)},
                'raw_children':len(transcript.children),'prompt_count':len(prompts),'response_count':len(responses)}
            if not hasattr(self,'scope_baseline'):self.scope_baseline=self.scope_observation
            # Only one observed placeholder allocation may change the transcript.
            handoff=None
            if hasattr(self,'base') and (identity[0]!=self.base[0] or
                not all(_same_ref(a,b) for a,b in zip(self.base[1:5],identity[1:5]))):
                if (self.submitted and not getattr(self,'handoff_used',False) and
                    allowed_placeholder_transition(self.scope_baseline,self.scope_observation)):
                    handoff={'old_identity':self.base,'before':self.scope_baseline,'after':self.scope_observation}
                    self.handoff_used=True
                    self.base=identity
                else:raise RelayFailure('siri_scope_replaced')
            if not hasattr(self,'base'):self.base=identity
            value=ax._get_attr_checked(composer.ref,'AXValue') or ''
            texts=['\n'.join(ax.extract_scoped_texts(ax.snapshot_tree(n.ref))) for n in prompts] if self.submitted and len(prompts)==1 else []
            ready=False
            if texts==[ticket.incoming.query] and len(responses)==1 and not value:
                try:region,signature,refs=self.snapshot_target(ticket)
                except ValueError as exc:raise RelayFailure('preview_read_failed') from exc
                ready=region is not None
                if ready:self.snapshot=(region,signature,refs)
            ticket.check()
            return Turn(identity,value,texts,len(responses),len(transcript.children),ready,
                        repr(self.snapshot[:2]) if ready else None,handoff)

    def submit(self,ticket,before):
        import ApplicationServices as A
        from .capture.deadline import bounded_ax_reads
        ticket.check()
        check=self.observe(ticket)
        if check.raw_children or check.composer or any(not _same_ref(a,b) for a,b in zip(before.identity[:5],check.identity[:5])):
            raise RelayFailure('pre_submit_interference')
        field=before.identity[3]
        with bounded_ax_reads(ticket.deadline):
            if A.AXUIElementSetAttributeValue(field,'AXValue',ticket.incoming.query):raise RelayFailure('write_failed')
            err,value=A.AXUIElementCopyAttributeValue(field,'AXValue',None)
            if err or value!=ticket.incoming.query:raise RelayFailure('composer_interference')
            ticket.check()
            if A.AXUIElementPerformAction(field,'AXConfirm'):raise RelayFailure('confirm_uncertain')
            self.submitted=True

    def extract_answer_text(self,response,bounds):
        return answer_above_preview(response,bounds)

    def capture(self,ticket,turn):
        from .capture import current
        from .capture.deadline import bounded_ax_reads
        before=self.snapshot
        path=ticket.directory/'preview.png'
        from .capture import ax
        with bounded_ax_reads(ticket.deadline):
            response=ax.snapshot_tree(before[2][4])
            signature=hashlib.sha256(json.dumps([dataclasses.asdict(n.element) for n in ax.nodes(response)],sort_keys=True).encode()).hexdigest()
            if signature!=before[1]:raise RelayFailure('preview_changed_before_text_read')
            text=self.extract_answer_text(response,before[0]['bounds'])
            options={'region_selector':self.region_selector} if self.region_selector else {}
            current.capture_current(self.backend,self.pid,ticket.incoming.query,path,deadline=ticket.deadline,**options)
            after=self.snapshot_target(ticket)
        if before[:2]!=after[:2] or not current.same_identity(before[2],after[2]):raise RelayFailure('preview_changed')
        data=path.read_bytes();validate_png(data);ticket.check()
        digest=hashlib.sha256(data).hexdigest()
        from .pixel_review import PixelReviewer
        reviewed=(self.reviewer(path,ticket,before[0]['title']) if isinstance(self.reviewer,PixelReviewer) else self.reviewer(path,ticket))
        if reviewed!=digest:raise RelayFailure('preview_unreadable')
        if hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise RelayFailure('attachment_changed')
        ticket.check()
        return Preview(ticket,text=text or None,image=path,scoped=True,fresh_turn=True,identity_verified=True,readable=True)

    def pause(self,ticket):
        ticket.check();time.sleep(min(.05,max(0,ticket.deadline-time.monotonic())))


def answer_above_preview(response,bounds):
    from .capture import ax,current
    lines=[]
    for node in ax.nodes(response):
        element=node.element
        if element.identifier=='modelResponse-richTextParagraph' and element.text:
            if not element.bounds or not current.contains(response.element.bounds,element.bounds):
                raise RelayFailure('answer_text_geometry_unverified')
            if element.bounds[1]+element.bounds[3]<=bounds[1]:lines.append(element.text)
    return '\n'.join(lines)
