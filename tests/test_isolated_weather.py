import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from diagnostics import isolated_weather_capture as experiment
from siri_cli.capture import current
from test_current_capture import win
from test_turn_scope import fails


def test_only_explicit_weather_spellings_are_accepted():
    assert experiment.exact_prompt("what's next")=="what's next"
    assert experiment.exact_prompt('what’s next')=='what’s next'
    for query in ('what is next'," what's next",'show sample study material'):
        fails(lambda:experiment.exact_prompt(query),ValueError)


def test_mismatch_exposes_whitespace_unicode_without_normalization():
    for value in ("what's next\n","what's\u00a0next",'what\u02bcs next',"what's next\u200b"):
        try:experiment.exact_prompt(value)
        except experiment.PromptMismatch as exc:
            o=exc.prompt_observation
            assert o['observed_prompt']==value and o['length']==len(value)
            assert o['unicode_escape']==value.encode('unicode_escape').decode('ascii')
            assert [c['codepoint'] for c in o['characters']]==[f'U+{ord(c):04X}' for c in value]
        else:raise AssertionError('mismatch must remain a mismatch')


def test_mismatch_is_saved_before_response_content_or_renderer_is_touched():
    root,composer,_,_,prompt,response,prior=paired_fixture();reads=[]
    value="what's next\u00a0"
    prompt.children[0].element.value=value
    def snapshot(ref,**kwargs):
        if kwargs.get('structural'):return root
        reads.append(ref)
        if ref is prompt.ref:return prompt
        raise AssertionError('read outside approved prompt gate')
    backend=SimpleNamespace(_find_pid=lambda:1,_windows=lambda app:[root.ref])
    with TemporaryDirectory() as tmp,patch.object(experiment.ax,'app_for_pid',return_value=object()), \
        patch.object(experiment.ax,'snapshot_tree',side_effect=snapshot), \
        patch.object(experiment.ax,'_get_attr_checked',return_value=None), \
        patch.object(current,'capture_window_region') as render:
        target=Path(tmp)/'preview.png'
        r=experiment.capture_experiment(backend,1,target,deadline=float('inf'),quartz=SimpleNamespace())
        render.assert_not_called()
        assert not target.exists() and not r['capture_attempted']
    assert reads==[prompt.ref] and r['image_status']=='failed'
    assert 'state' not in r and r['fresh_submission_verified'] is False
    o=r['prompt_observations'][0]
    assert o['observed_prompt']==value and o['segments']==[value]
    assert o['characters'][-1]=={'index':len(value)-1,'codepoint':'U+00A0',
        'name':'NO-BREAK SPACE','whitespace':True}
    assert 'PRIVATE' not in str(r)


def test_two_response_nodes_block_inspection_before_content_readback():
    from test_turn_scope import fixture,n
    _,composer,pane,transcript,_=fixture([n('AXStaticText','Weather')])
    transcript.children.append(n(identifier='modelResponse'))
    fails(lambda:experiment.structural_pair(transcript),ValueError)


def paired_fixture():
    from test_turn_scope import fixture,n
    root,composer,pane,transcript,_=fixture([])
    prompt=n(children=[n('AXStaticText','what’s next')],identifier='userPrompt')
    prior=n(children=[n('AXStaticText','PRIVATE EARLIER ANSWER')],identifier='modelResponse')
    card=n('AXCell',children=[n('AXStaticText',s) for s in experiment.EXPECTED_LABELS],
           identifier='Identifier:ResultCell',bounds=(10,110,200,80))
    response=n(children=[card],identifier='modelResponse',bounds=(0,100,300,300))
    def block(node):return n(children=[n('AXTextArea',children=[node])])
    provider=n('AXOpaqueProviderGroup',children=[block(prior),block(prompt),block(response)])
    transcript.children=[provider,n('AXScrollBar')]
    for node in experiment.ax.nodes(root):node.ref=object()
    return root,composer,transcript,provider,prompt,response,prior


def test_observed_provider_order_selects_only_following_response():
    _,_,t,_,prompt,response,_=paired_fixture()
    selected_prompt,selected_response,meta=experiment.structural_pair(t)
    assert selected_prompt is prompt and selected_response is response
    assert meta['ignored_prior_response_count']==1


def test_pair_selector_rejects_extra_following_response_or_intervening_block():
    from test_turn_scope import n
    for intervening in (True,False):
        _,_,t,provider,_,_,_=paired_fixture()
        if intervening:provider.children.insert(2,n())
        else:provider.children.append(n(identifier='modelResponse'))
        fails(lambda:experiment.structural_pair(t),ValueError)


def test_pair_selector_rejects_duplicate_prompt_and_response_in_following_block():
    from test_turn_scope import n
    for identifier in ('userPrompt','modelResponse'):
        _,_,t,provider,_,_,_=paired_fixture()
        provider.children[2].children.append(n(identifier=identifier))
        fails(lambda:experiment.structural_pair(t),ValueError)


def test_selected_pair_reads_neither_prior_response_nor_whole_pane_content():
    root,composer,t,_,prompt,response,prior=paired_fixture();reads=[]
    def snapshot(ref,**kwargs):
        reads.append((ref,kwargs.get('structural',False)))
        if kwargs.get('structural'):return root
        if ref is prompt.ref:return prompt
        if ref is response.ref:return response
        raise AssertionError('content read escaped selected pair')
    backend=SimpleNamespace(_windows=lambda app:[root.ref])
    with patch.object(experiment.ax,'app_for_pid',return_value=object()), \
         patch.object(experiment.ax,'snapshot_tree',side_effect=snapshot), \
         patch.object(experiment.ax,'_get_attr_checked',return_value=None), \
         patch.object(experiment.ax,'_element_bounds',return_value=(0,0,400,400)):
        result=experiment.read_target(backend,1)
    assert 'PRIVATE' not in str(result['state'])
    assert result['labels']==experiment.EXPECTED_LABELS
    assert all(ref is not prior.ref for ref,structural in reads if not structural)


def test_overlap_classification_preserves_production_rejection():
    windows=[win(2,9,(0,0,100,100)),win(1,1,(0,0,100,100))]
    quartz=SimpleNamespace(CGWindowListCopyWindowInfo=lambda *args:windows,
        kCGWindowListOptionOnScreenOnly=1,kCGWindowListExcludeDesktopElements=2,kCGNullWindowID=0)
    result=experiment.window_context(quartz,1,(10,10,20,20))
    assert result['window_id']==1 and result['overlapping_windows'][0]['window_id']==9
    fails(lambda:current.assert_unobscured(windows,1,(10,10,20,20)),current.CaptureObscured)


def sample():
    return {'window_ref':object(),'identity':tuple(object() for _ in range(5)),
        'region':{'bounds':(10,10,20,20)},'signature':'same','actual_prompt':'what’s next',
        'state':{'text':'Weather only'},'labels':experiment.EXPECTED_LABELS.copy()}


def execute(before,after,*,image=True,encoder_error=False,window_ids=(1,1)):
    calls=[];frames=iter([before,after]);contexts=iter([
        {'window_id':number,'overlapping_windows':[{'pid':2,'window_id':9}]} for number in window_ids])
    q=SimpleNamespace(CGImageSourceCreateWithURL=lambda *args:'source',
        CGImageSourceCreateImageAtIndex=lambda *args:'image' if image else None,
        CGImageGetWidth=lambda *args:40,CGImageGetHeight=lambda *args:40)
    def encode(pid,window,rect,path):
        calls.append((pid,window,rect));path.write_bytes(b'fake encoder boundary')
        if encoder_error:raise OSError('encoder unavailable')
    with TemporaryDirectory() as temp,patch.object(experiment,'read_target',side_effect=lambda *args,**kwargs:next(frames)), \
        patch.object(experiment,'window_context',side_effect=lambda *args:next(contexts)), \
        patch.object(current,'capture_window_region',side_effect=encode), \
        patch.dict(sys.modules,CoreFoundation=SimpleNamespace(CFEqual=lambda a,b:a is b),
                   Foundation=SimpleNamespace(NSURL=SimpleNamespace(fileURLWithPath_=lambda value:value))):
        target=Path(temp)/'preview.png'
        result=experiment.capture_experiment(SimpleNamespace(_find_pid=lambda:1),1,target,deadline=float('inf'),quartz=q)
        return result,calls,target.exists(),target.with_name('preview.rejected.png').exists()


def test_isolated_capture_targets_only_expected_window_and_result_rect():
    before=sample();result,calls,present,rejected=execute(before,before)
    assert calls==[(1,1,(10,10,20,20))] and present and not rejected
    assert result['image_status']=='captured_pending_visual_review'
    assert result['alias_used'] and result['completion']=='unverified'
    assert not result['fresh_submission_verified'] and not result['accepted_transaction']
    assert result['visual_readability']=='not_reviewed'


def test_isolated_capture_rejects_geometry_content_and_identity_changes():
    for key,value in [('region',{'bounds':(11,10,20,20)}),('signature','changed'),
                      ('identity',tuple(object() for _ in range(5))),('window_ref',object())]:
        before=sample();after={**before,key:value}
        result,_,present,rejected=execute(before,after)
        assert result['image_status']=='failed' and not present and rejected


def test_isolated_capture_refuses_unintended_weather_before_rendering():
    before=sample();before['labels']=['Different city','70°']
    result,calls,present,_=execute(before,before)
    assert result['image_status']=='failed' and not calls and not present


def test_invalid_image_and_encoder_failure_cannot_be_delivered():
    before=sample()
    for options in ({'image':False},{'encoder_error':True},{'window_ids':(1,2)}):
        result,_,present,rejected=execute(before,before,**options)
        assert result['image_status']=='failed' and not present and rejected
