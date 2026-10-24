import pytest
from siri_cli.response_kind import select_readable
from test_turn_scope import n


def calculator():
 p=n('AXStaticText','The answer is 4.',identifier='modelResponse-richTextParagraph',bounds=(469,256,111,18))
 group=n(children=[n('AXStaticText','2 + 2 =',bounds=(469,322,43,16)),n('AXStaticText','4',bounds=(469,338,16,31)),n('AXButton','Copy',actions=('AXPress',),bounds=(926,333,26,26))])
 cell=n('AXCell',children=[group],identifier='Identifier:ResultCell',bounds=(453,306,514,79))
 view=n(children=[n('AXScrollArea',children=[n('AXList',children=[n('AXList',children=[n(),cell,n()])])])],identifier='modelResponse-richTextCustomView')
 return n(children=[p,view],identifier='modelResponse',bounds=(439,256,542,141)),group


def test_observed_calculator_is_readable_text_not_a_file():
 r,g=calculator();result=select_readable(r,(439,200,559,600))
 assert result['kind']=='text-candidate' and result['subtype']=='calculator-card'
 assert result['text']=='The answer is 4.\n2 + 2 =\n4'
 assert 'completion' not in result


@pytest.mark.parametrize('mode',['progress','file_label','changed_copy','extra_image','clipped','incomplete'])
def test_lookalikes_and_incomplete_calculators_rejected(mode):
 r,g=calculator()
 if mode=='progress':g.children[1].element.value='Calculating'
 if mode=='file_label':g.children[0].element.value='result.pdf'
 if mode=='changed_copy':g.children[2].element.value='Open'
 if mode=='extra_image':g.children.append(n('AXImage'))
 if mode=='clipped':g.children[1].element.bounds=(469,800,16,31)
 if mode=='incomplete':r.complete=False
 with pytest.raises(ValueError):select_readable(r,(439,200,559,600))
