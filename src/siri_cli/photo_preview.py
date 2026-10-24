"""Opt-in reviewed photo candidate; selection never establishes intended identity."""
import hashlib
import json
import re
import subprocess
import struct
import sys
import time
from .capture import ax, current
from .preview_provider import NativePreviewPort
from .relay import RelayFailure, validate_png
from .preparation import StalePreparationMixin


class PhotoSelectionError(ValueError):
    def __init__(self, reason, response, transcript_bounds, conditions=None):
        super().__init__(reason)
        rows=[]
        def walk(node,path=()):
            if len(rows)>=256:return
            el=node.element
            rows.append({'path':list(path),'role':el.role,'identifier':el.identifier,
                         'bounds':el.bounds,'child_count':len(node.children),'actions':list(el.actions)})
            for i,child in enumerate(node.children):walk(child,path+(i,))
        # Metadata only; never AX reads, body/labels, filenames, URLs or pixels.
        if response.element.identifier=='modelResponse':walk(response)
        self.photo_rejection={'reason':reason,'conditions':conditions or {},
            'response_complete':response.complete,'transcript_bounds':transcript_bounds,
            'structure':rows,'possibly_truncated':len(rows)==256,
            'eligibility':'rejected_diagnostic_only'}


def photo_rejection_details(error):
    seen=set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error,PhotoSelectionError):return error.photo_rejection
        error=error.__cause__
    return None


def select_single_photo(response, transcript_bounds):
    if not response.complete:
        raise PhotoSelectionError('incomplete photo response',response,transcript_bounds)
    if response.element.identifier != 'modelResponse':
        raise PhotoSelectionError('photo selector requires the current response',response,transcript_bounds)
    views=[n for n in ax.nodes(response) if n.element.identifier=='modelResponse-richTextCustomView']
    if not views:
        raise current.PreviewNotReady('no embedded photo candidate')
    # Exactly the capture-6 shape. Other embedded widgets and mixed results are
    # deliberately unsupported, even if a generic selector could capture them.
    def paragraph(node):return node.element.role=='AXStaticText' and node.element.identifier=='modelResponse-richTextParagraph' and not node.children
    conditions={'supported_child_count':len(response.children) in (2,3),'one_custom_view':len(views)==1,
        'first_is_paragraph':bool(response.children) and paragraph(response.children[0]),
        'paragraph_is_leaf':bool(response.children) and not response.children[0].children,
        'second_is_unique_view':len(response.children)>1 and len(views)==1 and response.children[1] is views[0],
        'trailing_paragraph_only':len(response.children)==2 or len(response.children)==3 and paragraph(response.children[2])}
    if not all(conditions.values()):
        raise PhotoSelectionError('ambiguous or unsupported photo response',response,transcript_bounds,conditions)
    view=views[0]
    if (len(view.children)!=1 or view.children[0].element.role!='AXButton' or
        view.children[0].children or 'AXPress' not in view.children[0].element.actions):
        raise PhotoSelectionError('unique embedded photo button required',response,transcript_bounds)
    button=view.children[0];rect=button.element.bounds
    wrapper=view.element.bounds
    # Observed AX layout can round the wrapper's vertical origin one point
    # differently from its sole image button. Crop the button, which must
    # remain fully inside the verified response/transcript. Size and horizontal
    # alignment still match exactly; later capture snapshots remain exact.
    aligned=bool(rect and wrapper and rect[0]==wrapper[0] and
                 rect[2:]==wrapper[2:] and abs(rect[1]-wrapper[1])<=1)
    if (not aligned or rect[2]<120 or rect[3]<120 or
        not current.contains(transcript_bounds,rect) or
        not current.contains(response.element.bounds,rect)):
        raise PhotoSelectionError('photo geometry is clipped or unusable',response,transcript_bounds)
    return {'kind':'single-photo-candidate','bounds':rect,
            'title':'Single embedded photo candidate','requires_human_review':True}


select_single_photo.identity_paths=((1,),(1,0))


def ensure_photo_window(ticket):
    """One exact app launch if needed; no reset, query, or uncertain retry."""
    from .driver.siriai import SiriAiBackend,_SIRI_AI_APP
    from .capture.deadline import bounded_ax_reads
    backend=SiriAiBackend();ticket.check()
    pid=backend._find_pid()
    with bounded_ax_reads(ticket.deadline):
        windows=backend._windows(ax.app_for_pid(pid)) if pid is not None else []
    if not windows:
        result=subprocess.run(['/usr/bin/open','-a',_SIRI_AI_APP],
            capture_output=True,timeout=max(.001,min(5,ticket.deadline-time.monotonic())))
        if result.returncode:raise RelayFailure('siri_launch_failed')
        while True:
            ticket.check();pid=backend._find_pid()
            with bounded_ax_reads(ticket.deadline):
                windows=backend._windows(ax.app_for_pid(pid)) if pid is not None else []
            if windows:break
            time.sleep(min(.05,max(0,ticket.deadline-time.monotonic())))
    if len(windows)!=1:raise RelayFailure('unique_siri_window_required')
    ticket.check();backend._activate();ticket.check()


class PhotoPixelReviewer:
    """Conservative text legibility evidence; mandatory human review follows.

    This rejects text-free images for now. It does not recognize the desired
    subject, prove diagram legibility or replace the service's pixel review.
    """
    def __call__(self,path,ticket):
        ticket.check();data=path.read_bytes();validate_png(data)
        if min(struct.unpack('>II',data[16:24]))<120:
            raise RelayFailure('photo_pixels_too_small')
        result=subprocess.run([sys.executable,'-c',
            'import json,sys;from siri_cli.pixel_review import vision_lines;print(json.dumps(vision_lines(sys.argv[1])))',str(path)],
            capture_output=True,text=True,timeout=max(.001,min(10,ticket.deadline-time.monotonic())))
        if result.returncode:raise RelayFailure('photo_pixel_ocr_unavailable')
        lines=json.loads(result.stdout)
        words=re.findall(r'\w+',' '.join(text for text,confidence in lines if confidence>=.8))
        if len(words)<4:raise RelayFailure('photo_text_legibility_unverified')
        ticket.check()
        if path.read_bytes()!=data:raise RelayFailure('attachment_changed')
        return hashlib.sha256(data).hexdigest()


class ReviewedPhotoPort(StalePreparationMixin,NativePreviewPort):
    def prepare(self,ticket):
        # Never launch or activate anything for offline/default-disabled use.
        if self.live_enabled:
            ensure_photo_window(ticket)
        return super().prepare(ticket)

    def prepare_empty(self,ticket):
        try:
            return super().prepare_empty(ticket)
        except RelayFailure as error:
            if str(error)!='unique_composer_required' or self.submitted:
                raise
        # Observed launch state: conversation chooser, no current pane. Never
        # select a grid/history item. Open exactly one new conversation instead.
        from .capture.deadline import bounded_ax_reads
        from .capture.transaction import _same_ref
        import ApplicationServices as A
        with bounded_ax_reads(ticket.deadline):
            if self.backend._find_pid()!=self.pid:
                raise RelayFailure('preparation_process_replaced')
            windows=self.backend._windows(ax.app_for_pid(self.pid))
            if len(windows)!=1:raise RelayFailure('unique_siri_window_required')
            anchor=windows[0];root=ax.snapshot_tree(anchor,structural=True)
            all_nodes=list(ax.nodes(root))
            buttons=[n for n in all_nodes if n.element.identifier=='newChatButton'
                     and n.element.role=='AXButton' and 'AXPress' in n.element.actions]
            if (not root.complete or len(buttons)!=1 or
                not any(n.element.role=='AXOpaqueProviderGroup' for n in all_nodes) or
                any(n.element.identifier in ('innerChatSessionView','chatSessionView',
                    'promptViewTextField','userPrompt','modelResponse') or
                    'AXConfirm' in n.element.actions for n in all_nodes)):
                raise RelayFailure('unverified_conversation_chooser')
            ticket.check()
            if A.AXUIElementPerformAction(buttons[0].ref,'AXPress'):
                raise RelayFailure('new_conversation_action_uncertain')
        if hasattr(self,'base'):del self.base
        reacquired=False
        while True:
            ticket.check()
            try:current=self.observe(ticket)
            except ax.AXReadError as error:
                if error.error_code!=A.kAXErrorInvalidUIElement or reacquired:
                    raise
                with bounded_ax_reads(ticket.deadline):
                    if self.backend._find_pid()!=self.pid:
                        raise RelayFailure('preparation_process_replaced')
                    windows=self.backend._windows(ax.app_for_pid(self.pid))
                    if len(windows)!=1 or not _same_ref(windows[0],anchor):
                        raise RelayFailure('preparation_window_replaced')
                if hasattr(self,'base'):del self.base
                reacquired=True
                continue
            except RelayFailure as error:
                if str(error)!='unique_composer_required':raise
                self.pause(ticket);continue
            if current.identity[0]!=self.pid or not _same_ref(current.identity[1],anchor):
                raise RelayFailure('preparation_window_replaced')
            if current.composer or current.raw_children or current.prompts or current.response_count:
                raise RelayFailure('chooser_new_conversation_not_empty')
            return current

    def extract_answer_text(self,response,bounds):
        lines=[]
        for node in response.children:
            el=node.element
            if el.identifier=='modelResponse-richTextParagraph' and el.text:
                if not current.contains(response.element.bounds,el.bounds):
                    raise RelayFailure('answer_text_geometry_unverified')
                lines.append(el.text)
        return '\n'.join(lines)

    def __init__(self,*,live_enabled=False,exact_payload_review_required=False,reviewer=None):
        if exact_payload_review_required is not True:
            raise RelayFailure('photo_exact_payload_review_required')
        super().__init__(reviewer or PhotoPixelReviewer(),live_enabled=live_enabled,
                         region_selector=select_single_photo)


def worker_port_type(config):
    mode=config.get('preview_mode','standard')
    if mode=='standard':return NativePreviewPort
    if (mode!='single-photo-reviewed' or config.get('exact_payload_review_required') is not True or
        config.get('one_request_photo_test') is not True):
        raise RelayFailure('PHOTO_PREVIEW_UNVERIFIED')
    # The worker publishes only an authenticated candidate to the local outbox;
    # the existing service gate must approve its exact text+PNG before sending.
    class ScopedPhotoPort(ReviewedPhotoPort):
        def __init__(self,**kwargs):super().__init__(exact_payload_review_required=True,**kwargs)
    return ScopedPhotoPort
