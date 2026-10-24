import pytest
from siri_cli.response_kind import select_readable
from siri_cli.capture.current import PreviewNotReady
from test_turn_scope import n


def observed():
    return n(children=[
        n('AXStaticText','Exact answer.\nExact answer 3 ',identifier='modelResponse-richTextParagraph',bounds=(469,288,475,90)),
        n('AXGroup',identifier='modelResponse-richTextCustomView',bounds=(439,390,542,0)),
        n('AXButton','Source',identifier='modelResponse-richTextSourceAttribution',bounds=(467,390,102,21),actions=('AXPress',)),
        n('AXButton','More sources',identifier='modelResponse-richTextSourceAttribution',bounds=(569,390,21,21),actions=('AXPress',)),
    ],identifier='modelResponse',bounds=(439,288,542,123))


def test_observed_cloud_text_retains_exact_body_and_separate_source_controls():
    result=select_readable(observed(),(400,250,600,600))
    assert result['text']=='Exact answer.\nExact answer 3 '
    assert result['sources']==[{'label':'Source','url':None},{'label':'More sources','url':None}]
    assert result['kind']=='text-candidate'


@pytest.mark.parametrize('mode',['placeholder','clipped_source','incomplete','unrecognized_button','nested_view','positive_view'])
def test_pending_or_unverified_cloud_structure_cannot_be_accepted(mode):
    r=observed()
    if mode=='placeholder':r.children=r.children[:2]
    if mode=='clipped_source':r.children[2].element.bounds=(467,700,102,21)
    if mode=='incomplete':r.complete=False
    if mode=='unrecognized_button':r.children[2].element.identifier='other control'
    if mode=='nested_view':r.children[1].children=[n('AXImage',bounds=(439,390,542,200))]
    if mode=='positive_view':r.children[1].element.bounds=(439,390,542,20)
    with pytest.raises((ValueError,PreviewNotReady)):select_readable(r,(400,250,600,600))
