"""Capture readiness is distinct from generation completion."""
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from siri_cli.capture import current
from test_current_capture import win
from test_turn_scope import fails


def harness(snapshots,*,clock=None,windows=None):
    """Only the image encoder/AX boundary is replaced; capture guards run."""
    from contextlib import ExitStack
    stack=ExitStack()
    images=[]
    quartz=SimpleNamespace(kCGWindowListOptionOnScreenOnly=1,
        kCGWindowListExcludeDesktopElements=2,kCGNullWindowID=0,
        CGWindowListCopyWindowInfo=lambda *args:next(windows) if windows else [win(1,1,(0,0,100,100))],
        CGImageSourceCreateWithURL=lambda *args:'source',
        CGImageSourceCreateImageAtIndex=lambda *args:'image',
        CGImageGetWidth=lambda image:20,CGImageGetHeight=lambda image:20)
    foundation=SimpleNamespace(NSURL=SimpleNamespace(fileURLWithPath_=lambda value:value))
    def encode(pid,window,rect,target):
        images.append((pid,window,rect));target.write_bytes(b'fake PNG boundary')
    stack.enter_context(patch.dict(sys.modules,Quartz=quartz,Foundation=foundation))
    stack.enter_context(patch.object(current,'snapshot_target',side_effect=lambda *args:next(snapshots)))
    stack.enter_context(patch.object(current,'capture_window_region',side_effect=encode))
    if clock:stack.enter_context(patch.object(current.time,'monotonic',side_effect=lambda:clock[0]))
    return stack,images


def test_long_unchanged_progress_waits_for_preview_without_claiming_completion():
    refs=tuple(object() for _ in range(5));region={'bounds':(10,10,20,20),'title':'Study guide','kind':'note-preview'}
    # Matches the recorded failure mode: a long unchanged progress interval,
    # then a populated result. Neither duration nor population proves completion.
    snapshots=iter([(None,'progress',refs)]*4+[(region,'result',refs)]*2)
    clock=[0];stack,images=harness(snapshots,clock=clock)
    with stack,TemporaryDirectory() as tmp:
        result=current.capture_current(SimpleNamespace(_find_pid=lambda:1),1,'query',Path(tmp)/'preview.png',
            deadline=120,pump=lambda _:clock.__setitem__(0,clock[0]+20))
        assert clock[0]==80 and len(images)==1
        assert result['artifact_status']=='captured' and result['completion']=='unverified'
        assert result['fresh_submission_verified'] is False


def test_capture_can_wait_for_response_creation_on_the_bound_prompt():
    refs=tuple(object() for _ in range(5));region={'bounds':(10,10,20,20)}
    snapshots=iter([(None,None,(*refs[:4],None)),(None,'progress',refs),(region,'result',refs),(region,'result',refs)])
    stack,images=harness(snapshots)
    with stack,TemporaryDirectory() as tmp:
        result=current.capture_current(SimpleNamespace(_find_pid=lambda:1),1,'query',Path(tmp)/'preview.png',
            deadline=float('inf'),pump=lambda _:None)
        assert result['artifact_status']=='captured' and len(images)==1


def test_acknowledged_response_cannot_disappear_and_rebind():
    refs=tuple(object() for _ in range(5))
    stack,images=harness(iter([(None,'progress',refs),(None,None,(*refs[:4],None))]))
    with stack,TemporaryDirectory() as tmp:
        fails(lambda:current.capture_current(SimpleNamespace(_find_pid=lambda:1),1,'query',Path(tmp)/'preview.png',
            deadline=float('inf'),pump=lambda _:None),ValueError)
        assert not images


def test_unchanged_progress_times_out_without_an_image():
    refs=tuple(object() for _ in range(5));clock=[0]
    stack,images=harness(iter([(None,'progress',refs)]*20),clock=clock)
    with stack,TemporaryDirectory() as tmp:
        target=Path(tmp)/'preview.png'
        fails(lambda:current.capture_current(SimpleNamespace(_find_pid=lambda:1),1,'query',target,
            deadline=120,pump=lambda _:clock.__setitem__(0,clock[0]+30)),TimeoutError)
        assert not target.exists() and not images


def test_identical_query_on_replaced_turn_cannot_reuse_preview():
    refs=tuple(object() for _ in range(5));replacement=tuple(object() for _ in range(5))
    stack,images=harness(iter([(None,'progress',refs),({'bounds':(10,10,20,20)},'result',replacement)]))
    with stack,TemporaryDirectory() as tmp:
        fails(lambda:current.capture_current(SimpleNamespace(_find_pid=lambda:1),1,'same query',
            Path(tmp)/'preview.png',deadline=float('inf'),pump=lambda _:None),ValueError)
        assert not images


def test_late_response_update_rejects_the_written_candidate():
    refs=tuple(object() for _ in range(5));region={'bounds':(10,10,20,20)}
    stack,images=harness(iter([(region,'before',refs),(region,'after',refs)]))
    with stack,TemporaryDirectory() as tmp:
        target=Path(tmp)/'preview.png'
        fails(lambda:current.capture_current(SimpleNamespace(_find_pid=lambda:1),1,'query',target,deadline=float('inf')),ValueError)
        assert len(images)==1 and not target.exists()
        assert target.with_name('preview.rejected.png').exists()


def test_geometry_update_rejects_the_written_candidate():
    refs=tuple(object() for _ in range(5))
    stack,images=harness(iter([({'bounds':(10,10,20,20)},'same',refs),({'bounds':(15,10,20,20)},'same',refs)]))
    with stack,TemporaryDirectory() as tmp:
        target=Path(tmp)/'preview.png'
        fails(lambda:current.capture_current(SimpleNamespace(_find_pid=lambda:1),1,'query',target,deadline=float('inf')),ValueError)
        assert len(images)==1 and not target.exists()


def test_obstruction_appearing_during_capture_quarantines_candidate():
    refs=tuple(object() for _ in range(5));region={'bounds':(10,10,20,20)}
    windows=iter([[win(1,1,(0,0,100,100))],[win(2,2,(0,0,100,100)),win(1,1,(0,0,100,100))]])
    stack,images=harness(iter([(region,'same',refs)]*2),windows=windows)
    with stack,TemporaryDirectory() as tmp:
        target=Path(tmp)/'preview.png'
        fails(lambda:current.capture_current(SimpleNamespace(_find_pid=lambda:1),1,'query',target,deadline=float('inf')),current.CaptureObscured)
        assert len(images)==1 and not target.exists()


def test_capture_overrunning_deadline_cannot_deliver_its_image():
    refs=tuple(object() for _ in range(5));region={'bounds':(10,10,20,20)};clock=[0]
    stack,images=harness(iter([(region,'same',refs)]*2),clock=clock)
    def late_encoder(pid,window,rect,target):
        target.write_bytes(b'late image');clock[0]=121
    with stack,TemporaryDirectory() as tmp,patch.object(current,'capture_window_region',side_effect=late_encoder):
        target=Path(tmp)/'preview.png'
        fails(lambda:current.capture_current(SimpleNamespace(_find_pid=lambda:1),1,'query',target,deadline=120),TimeoutError)
        assert not target.exists() and target.with_name('preview.rejected.png').exists()


def test_final_pid_read_overrunning_deadline_cannot_deliver_its_image():
    refs=tuple(object() for _ in range(5));region={'bounds':(10,10,20,20)};clock=[0];calls=[0]
    def pid():
        calls[0]+=1
        if calls[0]==4:clock[0]=121
        return 1
    stack,images=harness(iter([(region,'same',refs)]*2),clock=clock)
    with stack,TemporaryDirectory() as tmp:
        target=Path(tmp)/'preview.png'
        fails(lambda:current.capture_current(SimpleNamespace(_find_pid=pid),1,'query',target,deadline=120),TimeoutError)
        assert len(images)==1 and not target.exists()
        assert target.with_name('preview.rejected.png').exists()


def test_preview_cannot_extend_from_response_into_prompt_or_other_content():
    from test_turn_scope import fixture,backend,n
    card=n('AXCell',children=[n('AXStaticText','Study guide.pdf')],
        identifier='Identifier:ResultCell',bounds=(60,10,20,20))
    root,composer,pane,transcript,_=fixture([card])
    transcript.element.bounds=(0,0,100,100)
    response=next(node for node in transcript.children if node.element.identifier=='modelResponse')
    response.element.bounds=(10,10,40,40)
    fails(lambda:current.snapshot_target(backend(root),1,'query'),ValueError)
