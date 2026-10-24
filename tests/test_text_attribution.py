import pytest
from siri_cli.capture.current import PreviewNotReady
from siri_cli.response_kind import select_readable
from test_turn_scope import n


def observed():
    paragraph=n('AXStaticText','Good morning.\nGood morning. 3',identifier='modelResponse-richTextParagraph',bounds=(469,256,143,18))
    attribution=n('AXUnknown',identifier='modelResponse-richTextSourceAttribution',bounds=(439,274,28,0),actions=('AXScrollToVisible',))
    return n(children=[paragraph,attribution],identifier='modelResponse',bounds=(439,256,542,18))


def test_observed_empty_attribution_preserves_exact_paragraph():
    result=select_readable(observed(),(400,200,600,700))
    assert result['kind']=='text-candidate' and result['text']=='Good morning.\nGood morning. 3'
    assert 'completion' not in result


@pytest.mark.parametrize('mode',['positive_height','labeled','nested','incomplete','different_id','file_url','outside','missing_bounds'])
def test_unproved_attribution_is_never_silently_ignored(mode):
    r=observed();aux=r.children[1]
    if mode=='positive_height':aux.element.bounds=(439,274,28,22)
    if mode=='labeled':aux.element.value='Important extra content'
    if mode=='nested':aux.children=[n('AXStaticText','Extra content')]
    if mode=='incomplete':r.complete=False
    if mode=='different_id':aux.element.identifier='unknown'
    if mode=='file_url':aux.element.url='file:///synthetic.pdf'
    if mode=='outside':aux.element.bounds=(439,275,28,0)
    if mode=='missing_bounds':aux.element.bounds=None
    with pytest.raises((ValueError,PreviewNotReady)):select_readable(r,(400,200,600,700))
