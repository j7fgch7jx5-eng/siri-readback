from siri_cli.capture.current import contains, select_region, assert_unobscured
from test_turn_scope import n, fails


def test_offscreen_card_is_rejected():
    response=n(children=[n('AXCell',identifier='Identifier:ResultCell',bounds=(0,90,50,30))],bounds=(0,0,50,120))
    fails(lambda:select_region(response,(0,0,100,100)),ValueError)


def test_note_preview_is_not_a_full_document():
    response=n(children=[n(children=[n('AXLink','Note title\ncontents',actions=('AXPress',))],identifier='modelResponse-richTextCustomView',bounds=(10,10,80,80))])
    result=select_region(response,(0,0,100,100))
    assert result['kind']=='note-preview' and result['title']=='Note title'


def test_pdf_card_preserves_accessible_full_title():
    response=n(children=[n('AXCell',children=[n('AXStaticText','Full study guide.pdf')],identifier='Identifier:ResultCell',bounds=(10,10,80,50))])
    result=select_region(response,(0,0,100,100))
    assert result['title']=='Full study guide.pdf'


def win(pid,number,bounds,layer=0):
    return {'kCGWindowOwnerPID':pid,'kCGWindowNumber':number,'kCGWindowLayer':layer,'kCGWindowBounds':dict(zip(('X','Y','Width','Height'),bounds))}


def test_obscured_target_is_rejected_even_with_single_window_capture():
    fails(lambda:assert_unobscured([win(2,2,(20,20,50,50)),win(1,1,(0,0,100,100))],1,(10,10,80,80)),ValueError)


def test_unobscured_target_window_is_selected():
    assert assert_unobscured([win(2,2,(200,200,50,50)),win(1,1,(0,0,100,100))],1,(10,10,80,80))==1


def test_ambiguous_window_refuses_capture():
    fails(lambda:assert_unobscured([win(1,1,(0,0,100,100)),win(1,2,(0,0,100,100))],1,(10,10,80,80)),ValueError)


def test_progress_only_response_is_not_a_result_capture():
    response=n(children=[n('AXStaticText','Searching…')],bounds=(10,10,80,30))
    fails(lambda:select_region(response,(0,0,100,100)),ValueError)


def test_partial_failed_capture_cannot_keep_deliverable_filename():
    import sys
    import time
    from pathlib import Path
    from tempfile import TemporaryDirectory
    from types import SimpleNamespace
    from unittest.mock import patch
    from siri_cli.capture import current
    quartz=SimpleNamespace(kCGWindowListOptionOnScreenOnly=1,
        kCGWindowListExcludeDesktopElements=2,kCGNullWindowID=0,
        CGWindowListCopyWindowInfo=lambda *args:[win(1,1,(0,0,100,100))])
    def failed_capture(pid,window,rect,target):
        target.write_bytes(b'partial')
        raise OSError('encoder failed')
    with TemporaryDirectory() as temp, patch.dict(sys.modules,Quartz=quartz), \
         patch.object(current,'snapshot_target',return_value=({'bounds':(10,10,20,20)},'signature',tuple(object() for _ in range(5)))), \
         patch.object(current,'capture_window_region',side_effect=failed_capture):
        target=Path(temp)/'result.png'
        backend=SimpleNamespace(_find_pid=lambda:1)
        fails(lambda:current.capture_current(backend,1,'query',target,deadline=time.monotonic()+120),OSError)
        assert not target.exists()
        assert target.with_name('result.rejected.png').is_file()


def test_note_does_not_need_a_press_action():
    response=n(children=[n(children=[n('AXLink','Study guide\nPreview text')],
        identifier='modelResponse-richTextCustomView',bounds=(10,10,80,80))])
    assert select_region(response,(0,0,100,100))['title']=='Study guide'


def test_static_native_preview_does_not_need_a_link():
    response=n(children=[n(children=[n('AXStaticText','Study guide'),n('AXStaticText','Preview text')],
        identifier='modelResponse-richTextCustomView',bounds=(10,10,80,80))])
    assert select_region(response,(0,0,100,100))['kind']=='embedded-result-preview'


def test_custom_view_with_only_a_progress_label_is_not_a_preview():
    response=n(children=[n(children=[n('AXStaticText','Searching documents')],
        identifier='modelResponse-richTextCustomView',bounds=(10,10,80,80))])
    fails(lambda:select_region(response,(0,0,100,100)),ValueError)


def test_result_order_uses_display_position_instead_of_tree_order():
    response=n(children=[n('AXCell',children=[n('AXStaticText','Second.pdf')],
        identifier='Identifier:ResultCell2',bounds=(10,60,80,20)),
        n('AXCell',children=[n('AXStaticText','First.pdf')],
        identifier='Identifier:ResultCell1',bounds=(10,10,80,20))])
    assert select_region(response,(0,0,100,100))['title']=='First.pdf'


def test_obstruction_reports_only_window_metadata():
    from siri_cli.capture.current import CaptureObscured
    import json
    overlay=win(2,9,(20,20,50,50));overlay['kCGWindowName']='private unrelated title'
    try:assert_unobscured([overlay,win(1,1,(0,0,100,100))],1,(10,10,80,80))
    except CaptureObscured as exc:
        assert exc.details['overlapping_windows'][0]['window_id']==9
        assert exc.details['window_id']==1
        assert 'private unrelated title' not in json.dumps(exc.details)
    else:raise AssertionError('obstruction must fail closed')


def test_dock_requires_positive_pixel_proof_and_other_overlays_still_block():
    from unittest.mock import patch
    from siri_cli.capture import current
    dock=win(2,9,(0,0,100,100),20);dock['kCGWindowOwnerName']='Dock'
    target=win(1,1,(0,0,100,100));rect=(10,10,80,80)
    with patch.object(current,'dock_pixels_unobscured',return_value=True) as proof:
        assert current.assert_unobscured([dock,target],1,rect)==1
        proof.assert_called_once()
        proof.reset_mock()
        fails(lambda:current.assert_unobscured([win(3,7,rect),dock,target],1,rect),ValueError)
        proof.assert_not_called()
    with patch.object(current,'dock_pixels_unobscured',return_value=False):
        fails(lambda:current.assert_unobscured([dock,target],1,rect),ValueError)


def test_dock_pixel_proof_checks_system_identity_and_all_four_samples():
    import sys
    from types import SimpleNamespace
    from unittest.mock import patch
    from siri_cli.capture.current import dock_pixels_unobscured
    def bundle(pid):return SimpleNamespace(bundleIdentifier=lambda:'com.apple.dock',
        bundleURL=lambda:SimpleNamespace(path=lambda:'/System/Library/CoreServices/Dock.app'))
    obstruction=[{'pid':2}]
    def run(samples,app=bundle):
        images=iter(samples)
        q=SimpleNamespace(kCGWindowListOptionIncludingWindow=1,kCGWindowListOptionOnScreenOnly=2,
            kCGNullWindowID=0,kCGWindowImageBoundsIgnoreFraming=4,CGRectMake=lambda *a:a,
            CGWindowListCreateImage=lambda *a:next(images),CGImageGetWidth=lambda im:20,
            CGImageGetHeight=lambda im:20,CGImageGetBitsPerPixel=lambda im:32,
            CGImageGetBitsPerComponent=lambda im:8,CGImageGetBytesPerRow=lambda im:80,
            CGImageGetBitmapInfo=lambda im:8194,CGImageGetColorSpace=lambda im:0,
            CGColorSpaceGetModel=lambda space:1,CGImageGetDataProvider=lambda im:im,
            CGDataProviderCopyData=lambda im:im)
        kit=SimpleNamespace(NSRunningApplication=SimpleNamespace(runningApplicationWithProcessIdentifier_=app))
        with patch.dict(sys.modules,Quartz=q,AppKit=kit):return dock_pixels_unobscured(obstruction,1,(0,0,20,20))
    assert run([b'pixels']*4)
    assert not run([b'pixels',b'covered',b'pixels',b'covered'])
    assert not run([b'pixels',b'pixels',b'changed',b'changed'])
    assert not run([b'pixels']*4,app=lambda pid:None)
    assert not run([None]*4)


def test_shareable_transparent_overlay_needs_pixel_proof_not_an_owner_allowlist():
    from unittest.mock import patch
    from siri_cli.capture import current
    target=win(1,1,(0,0,100,100));rect=(10,10,80,80)
    overlay=win(2,2,(0,0,100,100),3)
    overlay['kCGWindowSharingState']=1
    overlay['kCGWindowOwnerName']='Any application'
    with patch.object(current,'region_pixels_equal',return_value=True) as proof:
        assert current.assert_unobscured([overlay,target],1,rect)==1
        proof.assert_called_once_with(1,rect)
    with patch.object(current,'region_pixels_equal',return_value=False):
        fails(lambda:current.assert_unobscured([overlay,target],1,rect),ValueError)
    for state in (None,0,2):
        overlay['kCGWindowSharingState']=state
        with patch.object(current,'region_pixels_equal',return_value=True) as proof:
            fails(lambda:current.assert_unobscured([overlay,target],1,rect),ValueError)
            proof.assert_not_called()
