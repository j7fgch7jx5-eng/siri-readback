from types import SimpleNamespace as N
from unittest.mock import patch
import pytest
from siri_cli.capture import ax
from siri_cli.document_response import NativeExistingDocumentUI
from siri_cli.relay import RelayFailure
from test_turn_scope import fixture,n


@pytest.mark.parametrize('mode',['fresh_response','old_response','old_prompt','old_pane'])
def test_document_replacement_rejected_before_content_read(mode):
    root,c,pane,t,_=fixture(prompt='question',answer=[n('AXStaticText','secret replacement')])
    for node in ax.nodes(root):node.ref=object()
    window=next(node.ref for node in ax.nodes(root) if node.element.role=='AXWindow')
    prompt,response=t.children
    ui=NativeExistingDocumentUI.__new__(NativeExistingDocumentUI)
    ui.backend=N(_find_pid=lambda:1,_windows=lambda app:[window])
    refs=[window,pane.ref,t.ref,c.ref,prompt.ref,response.ref]
    old=None
    if mode=='fresh_response':ui.expected_binding=(1,window,pane.ref,c.ref,t.ref,prompt.ref,object())
    else:
        refs[{'old_response':5,'old_prompt':4,'old_pane':1}[mode]]=object()
        old={'pid':1,'window':window,'refs':refs}
    reads=[]
    def snapshot(ref,**kwargs):
        reads.append(ref)
        assert ref is window, 'replacement contents must not be read'
        assert kwargs.get('structural') is True
        return root
    with patch.object(ax,'app_for_pid',return_value=None),patch.object(ax,'snapshot_tree',side_effect=snapshot),patch('siri_cli.capture.transaction._same_ref',lambda a,b:a is b):
        with pytest.raises(RelayFailure,match='identity_mismatch|source_changed'):
            ui._source('question',old)
    assert reads==[window]


def test_text_replacement_names_failed_identity_without_reading_contents():
    from siri_cli.response_kind import read_bound_text
    root,c,pane,t,_=fixture(prompt='question',answer=[n('AXStaticText','replacement')])
    for node in ax.nodes(root):node.ref=object()
    window=next(node.ref for node in ax.nodes(root) if node.element.role=='AXWindow')
    prompt,response=t.children
    identity=(1,window,pane.ref,c.ref,t.ref,prompt.ref,object())
    events=[]
    port=N(backend=N(_find_pid=lambda:1,_windows=lambda app:[window]),emit=events.append)
    def snapshot(ref,**kwargs):
        assert ref is window and kwargs.get('structural') is True
        return root
    with patch.object(ax,'app_for_pid',return_value=None),patch.object(ax,'snapshot_tree',side_effect=snapshot),patch('siri_cli.capture.transaction._same_ref',lambda a,b:a is b):
        with pytest.raises(ValueError,match='text_identity_replaced') as caught:
            read_bound_text(N(check=lambda:None),port,identity)
    assert events==[caught.value.identity_failure]
    assert events[0]['same']==dict(window=True,pane=True,composer=True,transcript=True,prompt=True,response=False)
    assert events[0]['read_index']==0
