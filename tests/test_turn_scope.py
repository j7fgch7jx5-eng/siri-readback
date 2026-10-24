"""Regression fixtures model the observed Mac identifiers; no private history."""
from unittest.mock import patch
from siri_cli.capture.ax import (
    AXElement, AXNode, conversation_container, extract_scoped_texts,
    find_composer_node, nodes, probe_result_cards, transcript_container,
)
from siri_cli.driver.siriai import SiriAiBackend
from siri_cli.driver.base import CaptureError


def n(role='AXGroup', text=None, children=(), **kw):
    return AXNode(AXElement(role,text,None,None,**kw),list(children))


def fixture(answer=(), *, prompt='query', sidebar='old preview'):
    c=n('AXTextField',identifier='promptViewTextField',actions=('AXConfirm',))
    children=[] if prompt is None else [n(children=[n('AXStaticText',prompt)],identifier='userPrompt')]
    if answer is not None:
        children += [n(children=answer,identifier='modelResponse')]
    t=n('AXScrollArea',children=children,identifier='chatSessionView')
    pane=n(children=[t,n(children=[c],identifier='promptView')],identifier='innerChatSessionView')
    history=n('AXScrollArea',children=[n('AXButton',sidebar,identifier='gridItemView')],identifier='chatGridView')
    root=n('AXApplication',children=[n('AXWindow',children=[n(children=[n('AXSplitGroup',children=[history,pane])])])])
    return root,c,pane,t,history


def backend(root):
    b=SiriAiBackend()
    c=find_composer_node(root)
    pane=conversation_container(root,c)
    b._turn_container=lambda pid:(pane,c)
    return b


def fails(fn, typ=CaptureError):
    try: fn()
    except typ: return
    raise AssertionError('expected fail-closed error')


def test_unique_composer():
    root,c,*_=fixture(); assert find_composer_node(root) is c


def test_multiple_composers_rejected():
    root,c,*_=fixture(); root.children.append(n('AXTextField',actions=('AXConfirm',)))
    assert find_composer_node(root) is None


def test_real_nested_split_structure_excludes_sidebar():
    root,c,pane,*_=fixture(); assert conversation_container(root,c) is pane


def test_generic_window_child_is_not_scope():
    c=n('AXTextField',actions=('AXConfirm',)); root=n('AXWindow',children=[n(children=[c])])
    assert conversation_container(root,c) is None


def test_missing_window_rejected():
    root,c,pane,*_=fixture(); assert conversation_container(pane,c) is None


def test_sidebar_inside_named_pane_rejected_even_empty():
    root,c,pane,t,history=fixture(sidebar=None);pane.children.append(history)
    assert conversation_container(root,c) is None


def test_late_sidebar_updates_cannot_enter_scope():
    root,c,pane,t,history=fixture([n('AXStaticText','fresh')])
    history.children.append(n('AXStaticText','late unrelated preview'))
    assert backend(root).inspect_current_turn(1)['text']=='fresh'


def test_missing_transcript_rejected():
    root,c,pane,t,_=fixture();t.element.identifier=None
    assert conversation_container(root,c) is None


def test_duplicate_transcripts_rejected():
    root,c,pane,t,_=fixture();pane.children.append(n(identifier='chatSessionView'))
    assert conversation_container(root,c) is None


def test_depth_truncation_rejected():
    root,c,*_=fixture();root.complete=False
    assert conversation_container(root,c) is None


def test_model_response_only_no_query_or_duplicate_textarea():
    root,c,pane,t,_=fixture([n('AXStaticText','fresh')]);t.children.insert(0,n('AXTextArea','duplicate query and answer'))
    assert backend(root).inspect_current_turn(1,expected_query='query')['text']=='fresh'


def test_repeated_answer_equal_to_query_preserved():
    root,*_=fixture([n('AXStaticText','query')])
    assert backend(root).inspect_current_turn(1,expected_query='query')['text']=='query'


def test_adjacent_repetitions_and_numeric_lines_preserved():
    root,*_=fixture([n('AXStaticText','yes'),n('AXStaticText','yes'),n('AXStaticText','3')])
    assert backend(root).inspect_current_turn(1)['text']=='yes\nyes\n3'


def test_line_breaks_preserved():
    root,*_=fixture([n('AXStaticText','one\ntwo')])
    assert backend(root).inspect_current_turn(1)['text']=='one\ntwo'


def test_different_prompt_rejected():
    root,*_=fixture([n('AXStaticText','answer')],prompt='changed')
    fails(lambda:backend(root).inspect_current_turn(1,expected_query='query'))


def test_no_prompt_does_not_expose_orphan_response():
    root,*_=fixture([n('AXStaticText','stale')],prompt=None)
    assert backend(root).inspect_current_turn(1)['text'] is None


def test_multiple_turns_rejected():
    root,c,pane,t,_=fixture();t.children.append(n(identifier='userPrompt'))
    fails(lambda:backend(root).inspect_current_turn(1))


def test_multiple_responses_rejected():
    root,c,pane,t,_=fixture();t.children.append(n(identifier='modelResponse'))
    fails(lambda:backend(root).inspect_current_turn(1))


def test_scoped_text_overflow_rejected_not_truncated():
    fails(lambda:extract_scoped_texts(n(children=[n('AXStaticText','a'),n('AXStaticText','b')]),limit=1),ValueError)


def test_identifier_alone_is_not_file_payload():
    assert probe_result_cards(n(identifier='arbitrary'))==[]


def test_payload_attributes_retained():
    card=n('AXCell',filename='Guide.pdf',url='file:///tmp/Guide.pdf',document='Guide.pdf')
    assert probe_result_cards(card)==[card.element]


def test_result_card_reported_without_inventing_path():
    card=n('AXCell',children=[n('AXStaticText','Guide.pdf')],identifier='Identifier:ResultCell,Section:0',bounds=(1,2,300,60),actions=('AXPress',))
    root,*_=fixture([card]);cards=backend(root).probe_result_cards(1)
    assert len(cards)==1 and cards[0]['labels']==['Guide.pdf']
    assert not cards[0]['has_payload_attribute'] and cards[0]['url'] is None


def test_composer_metadata_not_reported_as_card():
    root,c,*_=fixture(); c.element.url='file:///tmp/not-a-result.pdf'
    assert backend(root).probe_result_cards(1)==[]


def test_progress_text_never_means_completion():
    root,*_=fixture([n('AXStaticText','Searching for exam')]);b=backend(root)
    with patch('siri_cli.driver.siriai.time.monotonic',side_effect=[0,1,2,3]),patch('siri_cli.driver.siriai.time.sleep'):
        fails(lambda:b._wait_for_response(1,3,echo='query'))


def test_static_rendered_card_never_means_completion():
    root,*_=fixture([n('AXStaticText','Guide.pdf')]);b=backend(root)
    with patch('siri_cli.driver.siriai.time.monotonic',side_effect=[0,1,2,3,4,5]),patch('siri_cli.driver.siriai.time.sleep'):
        fails(lambda:b._wait_for_response(1,5,echo='query'))


def test_echo_only_timeout_returns_no_answer():
    root,*_=fixture(None);b=backend(root)
    with patch('siri_cli.driver.siriai.time.monotonic',side_effect=[0,1]),patch('siri_cli.driver.siriai.time.sleep'):
        assert b._wait_for_response(1,1,echo='query') is None


def test_raw_freshness_rejects_filtered_names():
    for label in ('Copy','Today','3'):
        root,c,pane,t,_=fixture(None,prompt=None);t.children=[n('AXStaticText',label)]
        b=backend(root);b._new_conversation=lambda pid:True
        b._type_query_ax=lambda q: (_ for _ in ()).throw(AssertionError('must not submit'))
        fails(lambda:b._ask_once('query',1,1,0))


def test_failed_new_conversation_prevents_submission():
    b=SiriAiBackend();b._new_conversation=lambda pid:False
    b._type_query_ax=lambda q: (_ for _ in ()).throw(AssertionError('must not submit'))
    fails(lambda:b._ask_once('query',1,1,0))


def test_ask_does_not_automatically_retry():
    b=SiriAiBackend();b.health=lambda:{'accessibility':True};b._ensure_running=lambda:None;b._ensure_windowed=lambda:1
    calls=[]
    def once(*args): calls.append(args);raise CaptureError('uncertain')
    b._ask_once=once
    fails(lambda:b.ask('query'));assert len(calls)==1


def test_ax_leaf_no_value_is_empty_not_transport_failure():
    import sys
    from types import SimpleNamespace
    from siri_cli.capture import ax
    api=SimpleNamespace(kAXErrorAttributeUnsupported=-25205,kAXErrorNoValue=-25212,
                        AXUIElementCopyAttributeValue=lambda *args:(-25212,None))
    with patch.dict(sys.modules,ApplicationServices=api),patch.object(ax,'_element_to_snapshot',return_value=n().element):
        assert ax.snapshot_tree(object()).complete


def test_ax_transport_error_is_not_empty_freshness():
    import sys
    from types import SimpleNamespace
    from siri_cli.capture import ax
    api=SimpleNamespace(kAXErrorAttributeUnsupported=-25205,kAXErrorNoValue=-25212,
                        AXUIElementCopyAttributeValue=lambda *args:(-25204,None))
    with patch.dict(sys.modules,ApplicationServices=api),patch.object(ax,'_element_to_snapshot',return_value=n().element):
        fails(lambda:ax.snapshot_tree(object()),ValueError)


def test_ax_confirm_failure_never_falls_back_to_global_typing():
    import sys
    from types import SimpleNamespace
    root,c,*_=fixture(None,prompt=None);b=backend(root);b._find_pid=lambda:1
    b._type_query=lambda q: (_ for _ in ()).throw(AssertionError('global typing forbidden'))
    api=SimpleNamespace(AXUIElementSetAttributeValue=lambda *args:0,
                        AXUIElementPerformAction=lambda *args:-25204)
    with patch.dict(sys.modules,ApplicationServices=api),patch('siri_cli.driver.siriai.time.sleep'):
        fails(lambda:b._type_query_ax('query'))


def test_ax_set_failure_never_confirms():
    import sys
    from types import SimpleNamespace
    root,c,*_=fixture(None,prompt=None);b=backend(root);b._find_pid=lambda:1
    def unexpected(*args): raise AssertionError('must not confirm')
    api=SimpleNamespace(AXUIElementSetAttributeValue=lambda *args:-25204,
                        AXUIElementPerformAction=unexpected)
    with patch.dict(sys.modules,ApplicationServices=api):
        fails(lambda:b._type_query_ax('query'))


def test_ax_failed_new_conversation_press_is_not_success():
    import sys
    from types import SimpleNamespace
    api=SimpleNamespace(AXUIElementCopyAttributeValue=lambda raw,k,_:(0,{'AXRole':'AXButton','AXTitle':'New Conversation'}.get(k)),
                        AXUIElementPerformAction=lambda *args:-25204)
    with patch.dict(sys.modules,ApplicationServices=api):
        assert not SiriAiBackend()._find_and_press(object(),'New Conversation')
