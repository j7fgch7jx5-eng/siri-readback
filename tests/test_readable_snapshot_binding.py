from types import SimpleNamespace as N
import time
from contextlib import nullcontext
from unittest.mock import patch
import pytest
from siri_cli.capture import ax
from siri_cli.capture.transaction import SubmissionState
from siri_cli.local_port import LocalReadablePort
from siri_cli.preview_provider import observe_preview_turn
from siri_cli.relay import RelayFailure
from test_turn_scope import fixture,n


@pytest.mark.parametrize('mode',['unbound_loading','already_bound','changed_prompt'])
def test_two_snapshot_readiness_uses_actual_candidate_identity(mode):
    text=n('AXStaticText','The answer is 15.',identifier='modelResponse-richTextParagraph',bounds=(5,5,200,20))
    root,c,pane,t,_=fixture(prompt='question',answer=[text])
    for node in ax.nodes(root):node.ref=object()
    win=next(node.ref for node in ax.nodes(root) if node.element.role=='AXWindow')
    prompt,response=t.children;response.element.bounds=(0,0,400,200);t.element.bounds=(0,0,500,500)
    loading=object();positive=response.ref;original_prompt=prompt.ref;events=[];content_reads=[];window_reads=[]
    def snapshot(ref,**kwargs):
        if ref is win:
            window_reads.append(1)
            response.ref=loading if len(window_reads)==1 else positive
            if mode=='changed_prompt' and len(window_reads)>1:prompt.ref=object()
            return root
        content_reads.append(ref)
        if ref is original_prompt:return prompt
        if ref is positive:return response
        raise AssertionError('unverified contents read')
    port=LocalReadablePort(live_enabled=False,emit=events.append)
    port.pid=1;port.submitted=True;port.base=(1,win,pane.ref,c.ref,t.ref,None,None)
    port.backend=N(_find_pid=lambda:1,_windows=lambda app:[win])
    if mode=='already_bound':port.accepted=(1,win,pane.ref,c.ref,t.ref,prompt.ref,loading)
    ticket=N(check=lambda:None,incoming=N(query='question'),deadline=time.monotonic()+10)
    with patch('siri_cli.capture.deadline.bounded_ax_reads',lambda *args:nullcontext()),patch.object(ax,'app_for_pid',return_value=None),patch.object(ax,'snapshot_tree',side_effect=snapshot),patch.object(ax,'_get_attr_checked',return_value=''),patch.object(ax,'_element_bounds',return_value=t.element.bounds),patch('siri_cli.local_port._same_ref',lambda a,b:a is b),patch('siri_cli.preview_provider._same_ref',lambda a,b:a is b),patch('siri_cli.capture.transaction._same_ref',lambda a,b:a is b):
        if mode=='unbound_loading':
            turn=port.observe(ticket)
            state=SubmissionState('run','question',binding=port.base)
            observe_preview_turn(state,turn)
            assert turn.ready and state.binding[6] is positive and port.accepted[6] is positive
            assert positive in content_reads and loading not in content_reads
            assert events[-1]['phase']=='first_readable_response_allocation'
        else:
            with pytest.raises(RelayFailure):port.observe(ticket)
            if mode=='already_bound':assert positive not in content_reads
