"""Acceptance requires both verified response and a readable delivered artifact."""
from pathlib import Path
from tempfile import TemporaryDirectory
from acceptance_mac import acceptance_passed
from unittest.mock import patch
from io import StringIO
from types import SimpleNamespace
from siri_cli.driver.base import CaptureError
import acceptance_mac


def test_no_capture_means_no_acceptance():
    assert not acceptance_passed({'ask_success':True,'completion_verified':True})


def test_unverified_completion_means_no_acceptance():
    with TemporaryDirectory() as tmp:
        p=Path(tmp)/'capture.png';p.write_bytes(b'candidate')
        assert not acceptance_passed({'ask_success':True,'capture':str(p),'artifact_verified':True})


def test_failed_ask_with_candidate_capture_never_passes():
    with TemporaryDirectory() as tmp:
        p=Path(tmp)/'capture.png';p.write_bytes(b'candidate')
        assert not acceptance_passed({'ask_success':False,'completion_verified':True,'capture':str(p),'artifact_verified':True})


def test_missing_artifact_never_passes():
    assert not acceptance_passed({'ask_success':True,'completion_verified':True,'artifact_verified':True,'capture':'/does/not/exist'})


def test_failed_or_unverified_ask_has_no_alternate_capture_path():
    for error in (None, CaptureError('unverified completion')):
        with patch.object(acceptance_mac, 'SiriAiBackend') as backend, \
             patch.object(acceptance_mac, 'capture_current') as capture, \
             patch.object(Path, 'write_text'), patch('sys.stdout', new_callable=StringIO):
            backend.return_value.ask.side_effect=error
            backend.return_value.ask.return_value=SimpleNamespace(text='candidate')
            assert acceptance_mac._run('test-run') == 1
            capture.assert_not_called()
            backend.return_value.inspect_current_turn.assert_not_called()
