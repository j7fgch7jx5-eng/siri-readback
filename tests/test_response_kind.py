import pytest
from siri_cli.response_kind import select_readable,select_document
from siri_cli.capture.current import PreviewNotReady
from test_turn_scope import n

def response(children):return n('AXGroup',children=children,identifier='modelResponse',bounds=(0,0,400,300))
def paragraph(text='4'):return n('AXStaticText',text,identifier='modelResponse-richTextParagraph',bounds=(5,5,200,30))

def test_static_text_branches_without_waiting_for_file():
 r=response([paragraph()]);assert select_readable(r,(0,0,500,500))['kind']=='text-candidate'

def test_one_pdf_card_branches_to_document():
 cell=n('AXCell',children=[n('AXStaticText','sample-document.pdf')],identifier='Identifier:ResultCell,Section:0',actions=('AXPress',),bounds=(5,50,300,60))
 r=response([paragraph('Found it'),n(children=[cell],identifier='modelResponse-richTextCustomView')]);assert select_readable(r,(0,0,500,500))['kind']=='pdf-document'

@pytest.mark.parametrize('mode',['incomplete','multiple','clipped','wrong_type'])
def test_bad_document_card_rejected(mode):
 cell=n('AXCell',children=[n('AXStaticText','a.txt' if mode=='wrong_type' else 'a.pdf')],identifier='Identifier:ResultCell',actions=('AXPress',),bounds=(5,500 if mode=='clipped' else 50,300,60))
 r=response([cell])
 if mode=='incomplete':r.complete=False
 if mode=='multiple':r.children.append(cell)
 with pytest.raises(ValueError):select_document(r,(0,0,500,500))

def test_progress_indicator_is_not_rendered_text():
 with pytest.raises(PreviewNotReady):select_readable(response([paragraph('working'),n('AXProgressIndicator')]),(0,0,500,500))

def test_rendered_word_complete_has_no_completion_semantics():
 assert select_readable(response([paragraph('complete')]),(0,0,500,500))['kind']=='text-candidate'


def test_fresh_text_route_does_not_touch_document_or_clipboard(tmp_path):
 from types import SimpleNamespace as N
 from unittest.mock import patch
 from siri_cli.document_response import fresh_readable_request
 ui=N()
 ticket=N(request_id='separate-run')
 port=N(snapshot=({'kind':'text-candidate'},))
 with patch('siri_cli.document_response.submit_and_wait',return_value=(N(binding=('bound',)),None)), patch('siri_cli.response_kind.read_bound_text',return_value='4.\n4.') as read, patch('siri_cli.document_response.read_bound_document') as doc:
  result=fresh_readable_request(ticket,port,ui)
 assert result['text']=='4.\n4.' and result['completion']=='unverified'
 assert result['human_review_required'] and not result['strict_text_completion_verified']
 assert not result['sent'] and not result['messages_staged']
 doc.assert_not_called();read.assert_called_once()


def test_fresh_path_cannot_reuse_approved_prior_turn():
 from types import SimpleNamespace as N
 from siri_cli.document_response import submit_and_wait
 with pytest.raises(Exception,match='prior_turn_mode_forbidden'):
  submit_and_wait(None,None,N(allow_prior_turn=True))


def test_observed_siri_error_is_not_successful_text_or_document():
 from siri_cli.response_kind import SiriReportedError
 r=response([paragraph('I’m having a bit of trouble here. Please try again.')])
 with pytest.raises(SiriReportedError) as caught:select_readable(r,(0,0,500,500))
 assert caught.value.source_message=='I’m having a bit of trouble here. Please try again.'


def test_error_words_inside_other_text_are_preserved():
 r=response([paragraph('The phrase is: I’m having a bit of trouble here. Please try again.')])
 assert select_readable(r,(0,0,500,500))['kind']=='text-candidate'
