from diagnostics.photon.failure_details import provider_failure_details
from siri_cli.driver.base import CaptureError
from siri_cli.relay import RelayFailure


def test_window_root_failure_retains_checked_numeric_error_without_guessing_node():
    error=CaptureError('AX application children read failed (-25202)')
    detail=provider_failure_details(error)
    assert detail['ax_failure']['error_code']==-25202
    assert detail['ax_failure']['attribute']=='AXChildren'
    assert detail['ax_failure']['structural_path']==[]
    assert detail['ax_failure']['path_origin']=='application_root'
    child=provider_failure_details(CaptureError('AX application child role read failed (-25204)'))
    assert child['ax_failure']['structural_path'] is None


def test_checked_relay_reason_and_nested_window_error_survive_without_private_text():
    outer=RelayFailure('submission_uncertain')
    outer.__cause__=CaptureError('AX application children read failed (-25204)')
    result=provider_failure_details(outer)
    assert result['failure_reason']=='submission_uncertain'
    assert result['ax_failure']['error_code']==-25204
    assert provider_failure_details(CaptureError('unrelated private response'))=={
        'failure_reason':None,'ax_failure':None}
