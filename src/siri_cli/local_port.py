"""Request-scoped local readback. No transport, OCR, sender or completion inference."""
import dataclasses,hashlib,json
from contextlib import contextmanager
from .capture import ax,current
from .capture.transaction import _same_ref
from .photo_preview import ReviewedPhotoPort
from .relay import RelayFailure
from .response_kind import select_readable,read_bound_text,response_structure


@contextmanager
def activity_checked_reads(check,api):
    originals={}
    try:
        for name in ('AXUIElementCopyAttributeNames','AXUIElementCopyAttributeValue','AXUIElementCopyActionNames'):
            original=getattr(api,name);originals[name]=original
            def read(*args,_call=original):
                check();result=_call(*args);check();return result
            setattr(api,name,read)
        yield
    finally:
        for name,original in originals.items():setattr(api,name,original)


class AXRecovery:
    """At most one complete read reacquisition per run, preserving its owner.

    A failed or partial tree is discarded. Reacquisition never performs an
    action, resets a binding, or resubmits the request. Caller still validates
    every outer/prompt/positive-response identity on the completed read.
    """
    def __init__(self,backend,pid,window,emit=lambda event:None):
        self.backend=backend;self.pid=pid;self.window=window;self.emit=emit;self.used=False
    def call(self,read,check):
        try:return read()
        except Exception as error:
            failure=ax.ax_read_failure(error)
            if not failure or failure['error_code'] not in (-25202,-25200,-25204) or self.used:raise
            check()
            observed_pid=self.backend._find_pid();check()
            if observed_pid!=self.pid:raise RelayFailure('reacquisition_process_replaced') from error
            windows=self.backend._windows(ax.app_for_pid(self.pid))
            if len(windows)!=1 or not _same_ref(windows[0],self.window):raise RelayFailure('reacquisition_window_replaced') from error
            self.used=True;self.emit({'phase':'read_reacquisition','ax_failure':failure,'submission_repeated':False})
            check();return read()


class LocalReadablePort(ReviewedPhotoPort):
    def __init__(self,*,live_enabled=False,emit=lambda event:None):
        super().__init__(live_enabled=live_enabled,exact_payload_review_required=True)
        self.region_selector=select_readable;self.emit=emit;self.accepted=None;self.recovery=None
    def sample_surface(self,ticket,ref,label):
        # Optional state survey errors remain evidence, never completion proof.
        from .completion_surface import describe_surface
        import ApplicationServices as api
        ticket.check()
        evidence=describe_surface(api,ref)
        ticket.check()
        previous=getattr(self,'surface_evidence',{})
        if previous.get(label)!=evidence:
            self.emit({'phase':'observable_completion_surface','target':label,**evidence})
            self.surface_evidence=dict(previous,**{label:evidence})
    def observe(self,ticket):
        if self.recovery is None:
            windows=self.backend._windows(ax.app_for_pid(self.pid))
            if len(windows)!=1:raise RelayFailure('unique_siri_window_required')
            self.recovery=AXRecovery(self.backend,self.pid,windows[0],self.emit)
        self.readable_identity=None;self.locating_identity=None
        turn=self.recovery.call(lambda:super(LocalReadablePort,self).observe(ticket),ticket.check)
        if turn.ready:
            # NativePreviewPort locates a possible response, then snapshot_target
            # takes the actual readable snapshot. Bind the latter's handle,
            # never the earlier loading handle. All already pinned references
            # were checked before snapshot_target read any response contents.
            positive=self.readable_identity
            if (positive is None or positive[0]!=turn.identity[0] or
                    not all(_same_ref(a,b) for a,b in zip(positive[1:6],turn.identity[1:6]))):
                raise RelayFailure('readable_snapshot_scope_mismatch')
            if not _same_ref(positive[6],turn.identity[6]):
                self.emit({'phase':'first_readable_response_allocation','outer_and_prompt_same':True,
                           'prior_positive_response_bound':bool(self.accepted and self.accepted[6] is not None),
                           'submission_repeated':False})
            turn=dataclasses.replace(turn,identity=positive)
        if self.submitted:
            if turn.prompts and turn.prompts!=[ticket.incoming.query]:raise RelayFailure('exact_prompt_mismatch')
            if turn.prompts:
                old=self.accepted
                self.accepted=(*turn.identity[:6],turn.identity[6] if turn.ready else (old[6] if old else None))
        return turn
    def validate_observed_structure(self,identity,prompts,responses):
        if not self.submitted:return
        if len(prompts)>1 or len(responses)>1:raise RelayFailure('duplicate_current_turn')
        locating=getattr(self,'locating_identity',None)
        if locating is not None and (identity[0]!=locating[0] or
                not all(_same_ref(a,b) for a,b in zip(identity[1:6],locating[1:6]))):
            raise RelayFailure('readable_snapshot_scope_mismatch')
        self.locating_identity=identity
        if self.accepted:
            for old,new in zip(self.accepted[5:],identity[5:]):
                if old is not None and (new is None or not _same_ref(old,new)):raise RelayFailure('bound_turn_replaced')
    def snapshot_target(self,ticket):
        # Do not use backend._turn_container's content snapshot of the pane.
        # Locate with structure, validate all pinned references, then read only
        # the unique verified prompt and its immediate response.
        ticket.check()
        observed_pid=self.backend._find_pid();ticket.check()
        if observed_pid!=self.pid:raise RelayFailure('siri_process_replaced')
        windows=self.backend._windows(ax.app_for_pid(self.pid))
        if len(windows)!=1 or not _same_ref(windows[0],self.base[1]):raise RelayFailure('siri_window_replaced')
        root=ax.snapshot_tree(windows[0],structural=True);c=ax.find_composer_node(root)
        if c is None:raise RelayFailure('unique_composer_required')
        pane=ax.conversation_container(root,c)
        if pane is None:raise RelayFailure('unique_pane_required')
        t=ax.transcript_container(pane)
        ps=[n for n in ax.nodes(t) if n.element.identifier=='userPrompt'];rs=[n for n in ax.nodes(t) if n.element.identifier=='modelResponse']
        identity=(self.pid,windows[0],pane.ref,c.ref,t.ref,ps[0].ref if len(ps)==1 else None,rs[0].ref if len(rs)==1 else None)
        if not root.complete or len(ps)!=1 or len(rs)!=1:raise RelayFailure('incomplete_or_duplicate_current_turn')
        if not all(_same_ref(a,b) for a,b in zip(self.base[1:5],identity[1:5])):raise RelayFailure('siri_scope_replaced')
        self.validate_observed_structure(identity,ps,rs)
        if ax._get_attr_checked(c.ref,'AXValue'):raise RelayFailure('composer_interference')
        if '\n'.join(ax.extract_scoped_texts(ax.snapshot_tree(ps[0].ref)))!=ticket.incoming.query:raise RelayFailure('exact_prompt_mismatch')
        ticket.check();response=ax.snapshot_tree(rs[0].ref);ticket.check()
        structure=response_structure(response)
        if structure!=getattr(self,'last_structure',None):
            self.emit({'phase':'scoped_response_structure','structure':structure});self.last_structure=structure
        try:region=select_readable(response,ax._element_bounds(t.ref,reader=ax._get_attr_checked))
        except current.PreviewNotReady:region=None
        expected=getattr(self,'expected_kind','auto')
        if region is not None and expected=='document' and region['kind']=='text-candidate':region=None
        if region is not None and expected=='text' and region['kind']!='text-candidate':raise RelayFailure('expected_text_received_document')
        if region is not None:self.readable_identity=identity
        signature=hashlib.sha256(json.dumps([dataclasses.asdict(n.element) for n in ax.nodes(response)],sort_keys=True).encode()).hexdigest()
        return region,signature,(pane.ref,c.ref,t.ref,ps[0].ref,rs[0].ref)
    def read_text(self,ticket,port,identity):
        return self.recovery.call(lambda:read_bound_text(ticket,self,identity),ticket.check)
