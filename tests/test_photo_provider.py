import dataclasses
import hashlib
import json
import struct
import threading
import time
import zlib
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import patch
import pytest
from siri_cli.capture import ax,current
from siri_cli.photo_preview import select_single_photo,ReviewedPhotoPort,PhotoPixelReviewer,worker_port_type
from siri_cli.relay import Ticket,Incoming,RelayFailure
from test_turn_scope import n,fixture,backend
from test_current_capture import win


def recorded():
    rows=json.loads(Path('tests/fixtures/single-photo-shape.json').read_text())['structure']
    nodes={}
    for row in rows:
        node=n(row['role'],identifier=row['identifier'],bounds=tuple(row['bounds']),actions=tuple(row['actions']))
        node.ref=object();nodes[tuple(row['path'])]=node
        if row['path']:nodes[tuple(row['path'][:-1])].children.append(node)
    nodes[(0,)].element.value='A fresh answer.\nA fresh answer.'
    return nodes[()]


def scoped():
    response=recorded();root,c,p,t,h=fixture(response.children,prompt='new query')
    actual=t.children[1];actual.element.bounds=response.element.bounds;t.element.bounds=(0,0,1200,1200)
    for node in ax.nodes(root):node.ref=object()
    b=backend(root);b._find_pid=lambda:1
    return root,actual,t,b


def test_capture6_fixture_uses_current_bounds_not_saved_coordinates():
    response=recorded()
    for node in ax.nodes(response):
        x,y,w,h=node.element.bounds;node.element.bounds=(x+71,y+17,w,h)
    result=select_single_photo(response,(0,0,2000,2000))
    assert result['bounds']==response.children[1].element.bounds
    assert result['requires_human_review'] is True
    assert result['kind']=='single-photo-candidate'


@pytest.mark.parametrize('delta',[-2,-1,1,2])
def test_observed_wrapper_origin_rounding_uses_contained_button_not_expanded_crop(delta):
    response=recorded();view=response.children[1];button=view.children[0]
    x,y,w,h=button.element.bounds
    view.element.bounds=(x,y+delta,w,h)
    if abs(delta)==1:
        result=select_single_photo(response,(0,0,2000,2000))
        assert result['bounds']==button.element.bounds
    else:
        with pytest.raises(ValueError):select_single_photo(response,(0,0,2000,2000))


def test_wrapper_rounding_never_allows_button_outside_response():
    response=recorded();view=response.children[1];button=view.children[0]
    x,y,w,h=button.element.bounds;view.element.bounds=(x,y+1,w,h)
    response.element.bounds=(x,y+1,w,h)
    with pytest.raises(ValueError):select_single_photo(response,(0,0,2000,2000))


def test_rejected_shape_records_exact_conditions_through_exception_cause_without_body():
    from siri_cli.photo_preview import PhotoSelectionError,photo_rejection_details
    response=recorded();response.children[0].element.value='private response body must not be logged'
    response.children.append(n('AXStaticText','another private line',identifier='unknown-widget'))
    try:select_single_photo(response,(0,0,2000,2000))
    except PhotoSelectionError as error:
        wrapped=RelayFailure('preview_read_failed');wrapped.__cause__=error
    else:pytest.fail('rejection required')
    detail=json.loads(json.dumps(photo_rejection_details(wrapped)))
    assert detail['conditions']['trailing_paragraph_only'] is False
    assert detail['conditions']['one_custom_view'] is True
    assert [node['path'] for node in detail['structure']]==[[],[0],[1],[1,0],[2]]
    assert 'private' not in json.dumps(detail)
    assert detail['eligibility']=='rejected_diagnostic_only'


@pytest.mark.parametrize('mutation',['multiple','nested_image','extra_result','clipped','different_button_bounds','incomplete','wrong_response'])
def test_ambiguous_wrong_or_unusable_photo_shapes_rejected(mutation):
    r=recorded();view=r.children[1]
    if mutation=='multiple':view.children.append(n('AXButton'))
    if mutation=='nested_image':view.children[0].children.append(n('AXImage'))
    if mutation=='extra_result':r.children.append(n(identifier='modelResponse-richTextCustomView'))
    if mutation=='clipped':r.element.bounds=(0,0,10,10)
    if mutation=='different_button_bounds':view.children[0].element.bounds=(1,2,300,300)
    if mutation=='incomplete':r.complete=False
    if mutation=='wrong_response':r.element.identifier='sidebar-preview'
    with pytest.raises(ValueError):select_single_photo(r,(0,0,2000,2000))


def test_wrong_turn_and_duplicate_prompt_reject_before_selection():
    root,response,t,b=scoped()
    with pytest.raises(ValueError):current.snapshot_target(b,1,'different query',region_selector=select_single_photo)
    t.children.append(n(identifier='userPrompt',children=[n('AXStaticText','new query')]))
    with pytest.raises(ValueError):current.snapshot_target(b,1,'new query',region_selector=select_single_photo)


def test_photo_snapshot_pins_view_and_button_in_addition_to_response():
    root,response,t,b=scoped();first=current.snapshot_target(b,1,'new query',region_selector=select_single_photo)
    assert len(first[2])==7
    response.children[1].children[0].ref=object()
    after=current.snapshot_target(b,1,'new query',region_selector=select_single_photo)
    with patch('CoreFoundation.CFEqual',lambda a,b:a is b):assert not current.same_identity(first[2],after[2])


def test_observed_trailing_paragraph_accepts_single_region_and_preserves_all_answer_text():
    fixture=json.loads(Path('tests/fixtures/photo-with-trailing-paragraph.json').read_text())
    nodes={}
    for row in fixture['structure']:
        node=n(row['role'],identifier=row['identifier'],bounds=tuple(row['bounds']),actions=tuple(row['actions']))
        node.ref=object();nodes[tuple(row['path'])]=node
        if row['path']:nodes[tuple(row['path'][:-1])].children.append(node)
    root=nodes[()];nodes[(0,)].element.value='Exact answer\nExact answer'
    nodes[(2,)].element.value='Follow-up 3 3'
    result=select_single_photo(root,(0,0,2000,2000))
    port=ReviewedPhotoPort(exact_payload_review_required=True)
    assert port.extract_answer_text(root,result['bounds'])=='Exact answer\nExact answer\nFollow-up 3 3'
    assert result['bounds']==nodes[(1,0)].element.bounds
    # A trailing second result/button is still rejected, not treated as text.
    nodes[(2,)].element.role='AXButton'
    with pytest.raises(ValueError):select_single_photo(root,(0,0,2000,2000))


def test_photo_geometry_mutation_quarantines_actual_capture(tmp_path):
    root,response,t,b=scoped();target=tmp_path/'preview.png'
    q=N(kCGWindowListOptionOnScreenOnly=1,kCGWindowListExcludeDesktopElements=2,kCGNullWindowID=0,
        CGWindowListCopyWindowInfo=lambda *a:[win(1,1,(0,0,1200,1200))])
    def encode(pid,wid,rect,path):
        path.write_bytes(b'synthetic capture')
        response.children[1].element.bounds=(400,400,376,282)
    with patch.dict('sys.modules',Quartz=q),patch.object(current,'capture_window_region',encode):
        with pytest.raises(ValueError):current.capture_current(b,1,'new query',target,deadline=time.monotonic()+10,region_selector=select_single_photo)
    assert not target.exists() and target.with_name('preview.rejected.png').exists()


def synthetic_png():
    def chunk(k,b):return struct.pack('>I',len(b))+k+b+struct.pack('>I',zlib.crc32(k+b)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',120,120,8,2,0,0,0))+chunk(b'IDAT',zlib.compress((b'\0'+b'\xff\xff\xff'*120)*120))+chunk(b'IEND',b'')


def test_native_photo_port_capture_reuses_guards_and_returns_fresh_payload(tmp_path):
    root,response,t,b=scoped();port=ReviewedPhotoPort(exact_payload_review_required=True,reviewer=lambda p,t:hashlib.sha256(p.read_bytes()).hexdigest())
    port.backend=b;port.pid=1
    ticket=Ticket('fresh-run',Incoming('m','space','new query'),tmp_path,time.monotonic()+20,threading.Event())
    port.snapshot=port.snapshot_target(ticket);calls=[]
    def capture(backend,pid,query,path,**kwargs):
        assert kwargs['region_selector'] is select_single_photo
        assert kwargs['deadline']==ticket.deadline
        calls.append(1);path.write_bytes(synthetic_png())
    with patch.object(ax,'snapshot_tree',return_value=response),patch.object(current,'capture_current',capture):
        result=port.capture(ticket,None)
    assert result.ticket is ticket and result.image.parent==tmp_path and len(calls)==1
    assert result.text=='A fresh answer.\nA fresh answer.'
    assert result.image.read_bytes()==synthetic_png()


def test_photo_worker_mode_defaults_disabled_and_requires_both_test_and_review():
    from siri_cli.preview_provider import NativePreviewPort
    assert worker_port_type({}) is NativePreviewPort
    for c in [{},{'exact_payload_review_required':True},{'one_request_photo_test':True}]:
        with pytest.raises(RelayFailure):worker_port_type({'preview_mode':'single-photo-reviewed',**c})
    with pytest.raises(RelayFailure):ReviewedPhotoPort()
    cls=worker_port_type({'preview_mode':'single-photo-reviewed','exact_payload_review_required':True,'one_request_photo_test':True})
    assert cls(live_enabled=False).region_selector is select_single_photo


def test_pixel_readability_is_only_evidence_not_intended_identity(tmp_path):
    path=tmp_path/'synthetic.png';path.write_bytes(synthetic_png())
    ticket=Ticket('run',Incoming('m','s','different drawing'),tmp_path,time.monotonic()+20,threading.Event())
    with patch('siri_cli.photo_preview.subprocess.run',return_value=N(returncode=0,stdout=json.dumps([['unrelated but legible words',.99]]))):
        assert PhotoPixelReviewer()(path,ticket)==hashlib.sha256(path.read_bytes()).hexdigest()
    with patch('siri_cli.photo_preview.subprocess.run',return_value=N(returncode=0,stdout='[]')):
        with pytest.raises(RelayFailure):PhotoPixelReviewer()(path,ticket)
    ticket.cancel.set()
    with pytest.raises(RelayFailure):PhotoPixelReviewer()(path,ticket)
