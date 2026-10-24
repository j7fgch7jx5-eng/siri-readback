from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch
import sys
import pytest
from diagnostics.drawing_structure_once import StalePreparationPort
from siri_cli.preview_provider import NativePreviewPort, Turn
from siri_cli.capture.ax import AXReadError
from siri_cli.relay import RelayFailure


@pytest.mark.parametrize('variant', ['success','wrong_window','wrong_process','second_failure','nonempty','other_error','no_menu'])
@pytest.mark.parametrize('photo_provider',[False,True])
def test_reacquisition_is_single_complete_read_before_submission_only(variant,photo_provider):
    events=[];reads=[];menus=[];window=object()
    from siri_cli.photo_preview import ReviewedPhotoPort
    port=(ReviewedPhotoPort(live_enabled=False,exact_payload_review_required=True) if photo_provider else StalePreparationPort(live_enabled=False))
    port.submitted=False;port.base=(1,window)
    port.diagnostic_emit=events.append
    backend=SimpleNamespace(_open_new_conversation_menu=lambda app:menus.append(1),
        _find_pid=lambda:2 if variant=='wrong_process' else 1,
        _windows=lambda app:[object() if variant=='wrong_window' else window])
    port.backend=backend
    def failing_prepare(self,ticket):
        if variant!='no_menu':self.backend._open_new_conversation_menu(None)
        error=AXReadError('AXUIElementCopyAttributeNames', -25204 if variant=='other_error' else -25202)
        error.structural_path=(0,1)
        raise error
    def observe(ticket):
        reads.append(1)
        if variant=='second_failure':raise AXReadError('AXUIElementCopyAttributeNames',-25202)
        return Turn((1,window,None,None,None,None,None),'',[],0,raw_children=1 if variant=='nonempty' else 0)
    ticket=SimpleNamespace(check=lambda:None,deadline=100)
    with patch.object(NativePreviewPort,'prepare_empty',failing_prepare),patch.object(port,'observe',observe), \
         patch('siri_cli.capture.transaction._same_ref',lambda a,b:a is b), \
         patch('siri_cli.capture.deadline.bounded_ax_reads',lambda *a:nullcontext()), \
         patch('siri_cli.capture.ax.app_for_pid',lambda p:None), \
         patch.dict(sys.modules,ApplicationServices=SimpleNamespace(kAXErrorInvalidUIElement=-25202)):
        if variant=='success':assert port.prepare_empty(ticket).raw_children==0
        else:
            with pytest.raises((RelayFailure,AXReadError)):port.prepare_empty(ticket)
    assert len(menus)==(0 if variant=='no_menu' else 1)
    assert len(reads)<=1
    assert not port.submitted
