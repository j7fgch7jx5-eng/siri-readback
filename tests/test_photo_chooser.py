from contextlib import nullcontext
from types import SimpleNamespace as N
from unittest.mock import patch
import pytest
from siri_cli.photo_preview import ReviewedPhotoPort
from siri_cli.preparation import StalePreparationMixin
from siri_cli.preview_provider import Turn
from siri_cli.relay import RelayFailure
from test_turn_scope import n


@pytest.mark.parametrize('variant',['success','incomplete','duplicate','pane','confirm',
    'no_grid','nonempty','wrong_window','action_error','process_changed',
    'one_stale','second_stale'])
def test_photo_launch_chooser_opens_once_and_requires_same_empty_turn(variant):
    button=n('AXButton',identifier='newChatButton',actions=('AXPress',));button.ref=object()
    grid=n('AXOpaqueProviderGroup',children=[n('AXButton','private old preview',identifier='gridItemView')])
    root=n('AXWindow',children=[grid,button]);window=object();calls=[]
    if variant=='incomplete':root.complete=False
    if variant=='duplicate':root.children.append(button)
    if variant=='pane':root.children.append(n(identifier='innerChatSessionView'))
    if variant=='confirm':root.children.append(n('AXTextField',actions=('AXConfirm',)))
    if variant=='no_grid':root.children.remove(grid)
    port=ReviewedPhotoPort(exact_payload_review_required=True);port.pid=1;port.submitted=False
    port.backend=N(_find_pid=lambda:2 if variant=='process_changed' else 1,_windows=lambda app:[window])
    reads=[]
    def observe(t):
        reads.append(1)
        if variant=='second_stale' or variant=='one_stale' and len(reads)==1:
            from siri_cli.capture.ax import AXReadError
            raise AXReadError('AXUIElementCopyAttributeNames',-25202)
        return Turn((1,object() if variant=='wrong_window' else window,None,None,None,None,None),
                    '',[],0,raw_children=1 if variant=='nonempty' else 0)
    port.observe=observe
    ticket=N(check=lambda:None,deadline=100)
    app=N(kAXErrorInvalidUIElement=-25202,AXUIElementPerformAction=lambda ref,action:calls.append((ref,action)) or (1 if variant=='action_error' else 0))
    with patch.object(StalePreparationMixin,'prepare_empty',side_effect=RelayFailure('unique_composer_required')), \
         patch('siri_cli.capture.ax.app_for_pid',return_value=None), \
         patch('siri_cli.capture.ax.snapshot_tree',return_value=root), \
         patch('siri_cli.capture.deadline.bounded_ax_reads',lambda d:nullcontext()), \
         patch('siri_cli.capture.transaction._same_ref',lambda a,b:a is b), \
         patch.dict('sys.modules',ApplicationServices=app):
        if variant in ('success','one_stale'):assert port.prepare_empty(ticket).raw_children==0
        else:
            from siri_cli.capture.ax import AXReadError
            with pytest.raises((RelayFailure,AXReadError)):port.prepare_empty(ticket)
    assert len(calls)==int(variant in ('success','nonempty','wrong_window','action_error','one_stale','second_stale'))
    assert len(reads)<=2
    assert all(ref is button.ref and action=='AXPress' for ref,action in calls)


def test_photo_chooser_never_recovers_after_submission():
    port=ReviewedPhotoPort(exact_payload_review_required=True);port.submitted=True
    with patch.object(StalePreparationMixin,'prepare_empty',side_effect=RelayFailure('unique_composer_required')):
        with pytest.raises(RelayFailure,match='unique_composer_required'):port.prepare_empty(None)
