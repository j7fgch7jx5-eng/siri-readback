from unittest.mock import patch
from siri_cli.preview_provider import scope_changes

def test_scope_changes_names_each_boundary_without_text_or_handles():
 refs=(681,object(),object(),object(),object(),None,None)
 with patch('siri_cli.preview_provider._same_ref',lambda a,b:a is b):
  assert all(scope_changes(refs,refs).values())
  for index,name in enumerate(('pid','window','pane','composer','transcript')):
   changed=list(refs);changed[index]=999 if index==0 else object()
   result=scope_changes(refs,changed)
   assert result[name] is False and sum(result.values())==4
   assert all(isinstance(value,bool) for value in result.values())


def handoff_fixture():
 from siri_cli.preview_provider import Turn
 from siri_cli.capture.transaction import SubmissionState
 refs=(681,object(),object(),object(),object(),None,None)
 identity=(*refs[:4],object(),None,object())
 before={'phase':'before_submit','raw_children':0,'prompt_count':0,'response_count':0,'nodes':{'pane':{'path':[0,2]},'transcript':{'path':[0,2,0],'role':'AXUnknown','identifier':'chatSessionView'}}}
 after={'phase':'after_submit','raw_children':1,'prompt_count':0,'response_count':1,'nodes':{'pane':{'path':[0,2]},'transcript':{'path':[0,2,0],'role':'AXScrollArea','identifier':'chatSessionView'}},'same':{'pid':True,'window':True,'pane':True,'composer':True,'transcript':False}}
 return SubmissionState('new-run',"what's next",binding=refs),Turn(identity,'',[],1,handoff={'old_identity':refs,'before':before,'after':after})

def test_observed_placeholder_allocation_accepts_orphan_only_while_awaiting_prompt():
 from siri_cli.preview_provider import apply_placeholder_handoff
 with patch('siri_cli.preview_provider._same_ref',lambda a,b:a is b),patch('siri_cli.capture.transaction._same_ref',lambda a,b:a is b):
  state,turn=handoff_fixture();apply_placeholder_handoff(state,turn)
  assert state.observe('',[],1,identity=turn.identity)=='awaiting_acknowledgement'
  assert state.acknowledged is False and state.run_id=='new-run'
  prompt=object();identity=(*turn.identity[:5],prompt,turn.identity[6])
  assert state.observe('',["what's next"],1,identity=identity)=='generating'

def test_handoff_rejects_history_extra_turns_changed_paths_outer_scope_and_repeated_transition():
 from siri_cli.preview_provider import apply_placeholder_handoff
 from siri_cli.relay import RelayFailure
 from test_turn_scope import fails
 mutations=[lambda s,t:t.handoff['before'].update(raw_children=1),
  lambda s,t:t.handoff['before']['nodes']['transcript'].update(role='AXScrollArea'),
  lambda s,t:t.handoff['after']['nodes']['transcript'].update(path=[0,3,0]),
  lambda s,t:t.handoff['after'].update(prompt_count=2),
  lambda s,t:t.handoff['after']['same'].update(pane=False),
  lambda s,t:setattr(t,'identity',(999,*t.identity[1:])),
  lambda s,t:setattr(t,'composer','interference'),
  lambda s,t:setattr(s,'acknowledged',True),
  lambda s,t:setattr(s,'preview_handoff_used',True)]
 with patch('siri_cli.preview_provider._same_ref',lambda a,b:a is b):
  for change in mutations:
   state,turn=handoff_fixture();change(state,turn);fails(lambda:apply_placeholder_handoff(state,turn),RelayFailure)


def test_answer_above_image_preserves_repetitions_and_excludes_card_labels():
 from types import SimpleNamespace as N
 from siri_cli.preview_provider import answer_above_preview
 def node(text,bounds,identifier='modelResponse-richTextParagraph'):
  return N(element=N(identifier=identifier,text=text,bounds=bounds),children=[])
 root=node(None,(0,0,600,400),None)
 root.children=[node('81° 81°\nline two',(0,0,300,40)),node('81° 81°',(0,50,300,20)),node('card label',(0,150,200,20),None)]
 assert answer_above_preview(root,(0,100,600,100))=='81° 81°\nline two\n81° 81°'


def test_loading_response_replacement_is_provisional_but_positive_response_is_pinned():
 from siri_cli.preview_provider import observe_preview_turn,Turn
 from siri_cli.capture.transaction import SubmissionState
 from test_turn_scope import fails
 refs=(681,object(),object(),object(),object());prompt=object();loading=object();actual=object()
 state=SubmissionState('fresh',"what's next",binding=(*refs,None,None))
 with patch('siri_cli.capture.transaction._same_ref',lambda a,b:a is b):
  pending=Turn((*refs,None,loading),'',[],1,ready=False)
  assert observe_preview_turn(state,pending)=='awaiting_acknowledgement'
  assert state.binding[6] is None
  pending.prompts=["what's next"];pending.identity=(*refs,prompt,loading)
  assert observe_preview_turn(state,pending)=='generating'
  assert state.binding[5] is prompt and state.binding[6] is None
  ready=Turn((*refs,prompt,actual),'',["what's next"],1,ready=True)
  observe_preview_turn(state,ready);assert state.binding[6] is actual
  ready.identity=(*refs,prompt,object())
  fails(lambda:observe_preview_turn(state,ready),ValueError)
