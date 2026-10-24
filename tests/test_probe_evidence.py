from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import probe_cards
from siri_cli.capture.current import CaptureObscured
from siri_cli.driver.base import CaptureError


def test_capture_failure_does_not_erase_successful_readback():
    backend=SimpleNamespace(inspect_current_turn=lambda *args,**kw:{'text':'Current answer','completion':'unverified'})
    with patch.object(probe_cards,'capture_current',side_effect=CaptureObscured(1,(0,0,20,20),[{'window_id':9}])):
        result=probe_cards.collect_evidence(backend,1,'query',Path('unused.png'),deadline=120)
    assert result['readback_status']=='extracted' and result['state']['text']=='Current answer'
    assert result['artifact_status']=='failed' and result['capture_error_details']['window_id']==1
    assert result['completion']=='unverified' and result['accepted_transaction'] is False


def test_successful_capture_does_not_become_completion_or_fresh_submission():
    backend=SimpleNamespace(inspect_current_turn=lambda *args,**kw:{'text':'Current answer'})
    with patch.object(probe_cards,'capture_current',return_value={'path':'preview.png','completion':'unverified'}):
        result=probe_cards.collect_evidence(backend,1,'query',Path('unused.png'),deadline=120)
    assert result['artifact_status']=='captured' and result['completion']=='unverified'
    assert not result['fresh_submission_verified'] and not result['accepted_transaction']


def test_failed_scoped_read_never_invokes_capture():
    backend=SimpleNamespace(inspect_current_turn=lambda *args,**kw:(_ for _ in ()).throw(CaptureError('read failed')))
    with patch.object(probe_cards,'capture_current') as capture:
        result=probe_cards.collect_evidence(backend,1,'query',Path('unused.png'),deadline=120)
    assert result['readback_status']=='failed' and result['artifact_status']=='not_requested'
    capture.assert_not_called()
