from contextlib import nullcontext
from types import SimpleNamespace as N
from unittest.mock import patch
import pytest
from siri_cli.photo_preview import ensure_photo_window,ReviewedPhotoPort
from siri_cli.preview_provider import NativePreviewPort
from siri_cli.relay import RelayFailure


@pytest.mark.parametrize('variant',['existing','missing','windowless','ambiguous','launch_failed','cancelled'])
def test_photo_startup_launches_at_most_once_and_never_resets(variant):
    launches=[];activations=[];checks=[]
    def check():
        checks.append(1)
        if variant=='cancelled':raise RelayFailure('cancelled')
    ticket=N(check=check,deadline=100)
    backend=N(_find_pid=lambda:None if variant=='missing' and not launches else 9,
        _windows=lambda app:[] if variant=='windowless' and not launches else ([object(),object()] if variant=='ambiguous' else [object()]),
        _activate=lambda:activations.append(1))
    def launch(args,**kwargs):
        launches.append(args);return N(returncode=1 if variant=='launch_failed' else 0)
    if variant=='launch_failed':backend._find_pid=lambda:None
    with patch('siri_cli.driver.siriai.SiriAiBackend',return_value=backend), \
         patch('siri_cli.capture.ax.app_for_pid',lambda pid:None), \
         patch('siri_cli.capture.deadline.bounded_ax_reads',lambda d:nullcontext()), \
         patch('siri_cli.photo_preview.subprocess.run',side_effect=launch), \
         patch('siri_cli.photo_preview.time.monotonic',return_value=1):
        if variant in ('ambiguous','launch_failed','cancelled'):
            with pytest.raises(RelayFailure):ensure_photo_window(ticket)
        else:ensure_photo_window(ticket)
    assert len(launches)==int(variant in ('missing','windowless','launch_failed'))
    assert len(activations)==int(variant in ('existing','missing','windowless'))
    if launches:assert launches[0]==['/usr/bin/open','-a','/System/Applications/Siri AI.app']


def test_photo_startup_disabled_path_cannot_launch():
    port=ReviewedPhotoPort(live_enabled=False,exact_payload_review_required=True)
    with patch('siri_cli.photo_preview.ensure_photo_window') as start, \
         patch.object(NativePreviewPort,'prepare',side_effect=RelayFailure('live_siri_disabled')):
        with pytest.raises(RelayFailure):port.prepare(None)
        start.assert_not_called()
