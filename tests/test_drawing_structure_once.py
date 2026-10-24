import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from diagnostics.drawing_structure_once import QUERY, classify, probe
from diagnostics.drawing_structure_once import ExistingEmptyPort
from siri_cli.preview_provider import Turn
from siri_cli.relay import Ticket, Incoming, RelayFailure


def ticket(tmp_path):
    return Ticket('test-run', Incoming('m', 'local', QUERY), tmp_path,
                  time.monotonic()+120, threading.Event())


def test_no_approval_no_output_or_query(tmp_path):
    output=tmp_path/'untouched'
    r=subprocess.run([sys.executable,'diagnostics/drawing_structure_once.py','--output',str(output)],capture_output=True,text=True)
    assert r.returncode!=0 and not output.exists()
    assert 'owner go-ahead required' in r.stderr


def test_nonempty_preparation_never_submits(tmp_path):
    port=SimpleNamespace(prepare=lambda t:Turn((),'',[],1,raw_children=1),submit=lambda *a:pytest.fail('submitted'))
    with pytest.raises(RelayFailure,match='fresh_empty_turn_required'):
        probe(ticket(tmp_path),port,lambda e:None)


@pytest.mark.parametrize('turn', [Turn((), 'in use', [], 0), Turn((), '', [], 1),
                                Turn((), '', [], 0, raw_children=1)])
def test_existing_empty_mode_aborts_without_menu_or_submission(tmp_path, turn):
    port=ExistingEmptyPort(live_enabled=False)
    with patch.object(port, 'observe', return_value=turn):
        with pytest.raises(RelayFailure,match='existing_empty_pane_required_no_menu_action'):
            port.prepare_empty(ticket(tmp_path))


def test_existing_empty_mode_returns_empty_observation_without_menu(tmp_path):
    port=ExistingEmptyPort(live_enabled=False);empty=Turn((), '', [], 0)
    with patch.object(port, 'observe', return_value=empty) as observe:
        assert port.prepare_empty(ticket(tmp_path)) is empty
        observe.assert_called_once()


def test_uncertain_submission_is_attempted_once(tmp_path):
    calls=[];identity=tuple(object() for _ in range(5))+(None,None)
    def submit(*args):
        calls.append(1);raise TimeoutError()
    port=SimpleNamespace(prepare=lambda t:Turn(identity,'',[],0),submit=submit)
    with pytest.raises(RelayFailure,match='submission_uncertain_no_retry'):
        probe(ticket(tmp_path),port,lambda e:None)
    assert calls==[1]


def test_orphan_loading_response_is_not_inspected_before_exact_prompt(tmp_path):
    outer=tuple(object() for _ in range(5));prompt=object();response=object()
    turns=iter([Turn(outer+(None,object()),'',[],1),
                Turn(outer+(prompt,response),'',[QUERY],1,ready=True),
                Turn(outer+(prompt,response),'',[QUERY],1,ready=True),
                Turn(outer+(prompt,response),'',[QUERY],1,ready=True)])
    class Port:
        response=None
        transcript=SimpleNamespace(ref=object())
        def prepare(self,t):return Turn(outer+(None,None),'',[],0)
        def submit(self,*a):pass
        def observe(self,t):
            turn=next(turns);self.response=SimpleNamespace(ref=turn.identity[-1]);return turn
    events=[];reads=[]
    def describe(node,*args):
        reads.append(node.ref)
        return [{'role':'AXImage','fully_in_transcript':True,'fully_in_response':True}]
    with patch('diagnostics.drawing_structure_once.describe_response',side_effect=describe),patch('siri_cli.capture.ax._element_bounds',return_value=(0,0,100,100)):
        result=probe(ticket(tmp_path),Port(),events.append,observation_seconds=0)
    assert reads==[response]
    assert result['pixel_identity']=='unproved' and result['completion']=='unverified'
    assert any(e['phase']=='current_response_structure' for e in events)


def test_identity_difference_saved_before_abort_without_replacement_text_read(tmp_path):
    outer=tuple(object() for _ in range(5));prompt=object();a=object();b=object()
    turns=iter([Turn(outer+(prompt,a),'',[QUERY],1),Turn(outer+(prompt,b),'',[QUERY],1)])
    class Port:
        response=None
        transcript=SimpleNamespace(ref=object())
        def prepare(self,t):return Turn(outer+(None,None),'',[],0)
        def submit(self,*a):pass
        def observe(self,t):
            turn=next(turns);self.response=SimpleNamespace(ref=turn.identity[-1]);self.scope_snapshot={'test_response': 'a' if turn.identity[-1] is a else 'b'};return turn
        def read_verified_text(self):pytest.fail('unverified replacement text read')
    events=[]
    with patch('diagnostics.drawing_structure_once.describe_response',return_value=[]),patch('siri_cli.capture.ax._element_bounds',return_value=(0,0,100,100)):
        with pytest.raises(RelayFailure,match='response_changed_during_structure_read'):
            probe(ticket(tmp_path),Port(),events.append,observation_seconds=0)
    pair=next(e for e in events if e['phase']=='structure_read_pair')
    assert pair['before']=={'test_response':'a'} and pair['after']=={'test_response':'b'}
    assert pair['same']['response'] is False
    assert all(v for k,v in pair['same'].items() if k!='response')


def test_gallery_and_clipped_image_are_not_single_image_candidates():
    image={'role':'AXImage','fully_in_transcript':True,'fully_in_response':True}
    assert classify([image,image])=='gallery_or_collection_requires_review'
    assert classify([image,{'role':'AXGrid'}])=='gallery_or_collection_requires_review'
    assert classify([{**image,'fully_in_response':False}])=='image_geometry_unusable'
    assert classify([])=='no_accessible_image_identified'
