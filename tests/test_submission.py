from siri_cli.capture.transaction import SubmissionState
from test_turn_scope import fails
from unittest.mock import patch


def test_confirm_may_leave_submitted_query_until_acknowledged():
    state = SubmissionState('run-a', 'query')
    assert state.observe('query', [], 0) == 'awaiting_acknowledgement'
    assert state.observe('query', ['query'], 0) == 'awaiting_acknowledgement'
    assert state.observe('', ['query'], 1) == 'generating'
    assert state.observe('', ['query'], 1) == 'generating'


def test_composer_reentry_even_matching_query_is_interference():
    state = SubmissionState('run-a', 'query')
    state.observe('', ['query'], 0)
    fails(lambda: state.observe('query', ['query'], 1), ValueError)


def test_different_pending_composer_value_is_interference():
    state = SubmissionState('run-a', 'query')
    fails(lambda: state.observe('different', [], 0), ValueError)


def test_duplicate_or_changed_turns_are_rejected():
    for prompts, count in [(['query', 'query'], 1), (['different'], 1), (['query'], 2), ([], 1)]:
        state = SubmissionState('run-a', 'query')
        fails(lambda: state.observe('', prompts, count), ValueError)


def test_prompt_disappearance_is_interruption():
    state = SubmissionState('run-a', 'query')
    state.observe('', ['query'], 0)
    fails(lambda: state.observe('', [], 0), ValueError)


def test_identical_query_has_independent_acknowledgement():
    first = SubmissionState('run-a', 'query')
    second = SubmissionState('run-b', 'query')
    first.observe('', ['query'], 1)
    assert first.run_id != second.run_id
    assert not second.acknowledged


def bound_state():
    refs=(681,object(),object(),object(),object(),None,None)
    return SubmissionState('weather-ordering','what’s weather',binding=refs),refs


def test_inferred_weather_response_before_prompt_waits_for_bound_acknowledgement():
    # Derived from the saved error (one response, no prompt), not a claim that
    # the missing triggering snapshot was recorded or that later readback is fresh.
    state,refs=bound_state();response=object();prompt=object()
    assert state.observe('',[],1,identity=(*refs[:5],None,response))=='awaiting_acknowledgement'
    assert not state.acknowledged
    assert state.observe('', ['what’s weather'],1,identity=(*refs[:5],prompt,response))=='generating'
    assert state.acknowledged


def test_bound_orphan_never_acknowledges_just_because_time_passes():
    state,refs=bound_state();response=object()
    for _ in range(300):
        assert state.observe('',[],1,identity=(*refs[:5],None,response))=='awaiting_acknowledgement'
    assert not state.acknowledged


@patch('siri_cli.capture.transaction._same_ref',lambda a,b:a is b)
def test_bound_submission_rejects_each_replaced_owner_or_scope():
    for index in range(5):
        state,refs=bound_state();changed=list(refs);changed[index]=999 if index==0 else object()
        fails(lambda:state.observe('',[],0,identity=tuple(changed)),ValueError)


@patch('siri_cli.capture.transaction._same_ref',lambda a,b:a is b)
def test_response_seen_before_prompt_cannot_be_replaced_or_disappear():
    for replacement in (None,object()):
        state,refs=bound_state();state.observe('',[],1,identity=(*refs[:5],None,object()))
        fails(lambda:state.observe('',[],int(replacement is not None),identity=(*refs[:5],None,replacement)),ValueError)


@patch('siri_cli.capture.transaction._same_ref',lambda a,b:a is b)
def test_bound_prompt_replacement_and_disappearance_are_interruptions():
    for replacement in (None,object()):
        state,refs=bound_state();response=object()
        state.observe('', ['what’s weather'],1,identity=(*refs[:5],object(),response))
        fails(lambda:state.observe('', ['what’s weather'] if replacement else [],1,
            identity=(*refs[:5],replacement,response)),ValueError)


def test_binding_cannot_be_added_after_submission_or_omit_continuity():
    state,refs=bound_state()
    fails(lambda:state.observe('',[],1),ValueError)
    unbound=SubmissionState('unbound','what’s weather')
    fails(lambda:unbound.observe('',[],1,identity=(*refs[:5],None,object())),ValueError)


def test_pre_submission_binding_cannot_contain_a_preexisting_turn():
    _,refs=bound_state()
    fails(lambda:SubmissionState('run','query',binding=(*refs[:5],object(),None)),ValueError)


def test_ordering_tolerance_preserves_composer_interference_check():
    state,refs=bound_state();response=object()
    state.observe('',[],1,identity=(*refs[:5],None,response))
    fails(lambda:state.observe('what’s weather',[],1,identity=(*refs[:5],None,response)),ValueError)
