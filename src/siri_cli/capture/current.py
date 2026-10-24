"""Current-response diagnostic captures; never assert answer completion."""
from __future__ import annotations
import dataclasses
import hashlib
import json
from pathlib import Path
import time
from . import ax


class PreviewNotReady(ValueError):
    """The bound response has not exposed a usable preview yet."""


class CaptureObscured(ValueError):
    def __init__(self, window_id, rect, windows):
        super().__init__('another window obscures the result region')
        self.details = {'window_id': window_id, 'bounds': rect,
                        'overlapping_windows': windows}


def contains(outer, inner):
    if not outer or not inner:return False
    x,y,w,h=inner;ox,oy,ow,oh=outer
    return w>0 and h>0 and ox<=x and oy<=y and x+w<=ox+ow and y+h<=oy+oh


def overlaps(a,b):
    x,y,w,h=a;u,v,p,q=b
    return min(x+w,u+p)>max(x,u) and min(y+h,v+q)>max(y,v)


def dock_pixels_unobscured(obstructions, window_id, rect):
    """Exempt only a genuine system Dock surface proven pixel-transparent here.

    No owner-name-only allowlist. No screenshots are saved. Any unknown owner,
    unreadable pixels, differing formats or image change fails closed.
    """
    try:
        from AppKit import NSRunningApplication
        for obstruction in obstructions:
            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(obstruction['pid'])
            if (app is None or str(app.bundleIdentifier()) != 'com.apple.dock' or
                str(app.bundleURL().path()) != '/System/Library/CoreServices/Dock.app'):
                return False
        return region_pixels_equal(window_id,rect)
    except Exception:
        return False


def region_pixels_equal(window_id,rect):
    """Prove only this region unchanged across isolated/composited samples."""
    try:
        import Quartz as Q
        fingerprints = []
        for option, number in ((Q.kCGWindowListOptionIncludingWindow, window_id),
                               (Q.kCGWindowListOptionOnScreenOnly, Q.kCGNullWindowID)) * 2:
            image = Q.CGWindowListCreateImage(Q.CGRectMake(*rect), option, number,
                                            Q.kCGWindowImageBoundsIgnoreFraming)
            if image is None:
                return False
            width, height = Q.CGImageGetWidth(image), Q.CGImageGetHeight(image)
            data = bytes(Q.CGDataProviderCopyData(Q.CGImageGetDataProvider(image)))
            if width <= 0 or height <= 0 or not data:
                return False
            fingerprints.append((width, height, Q.CGImageGetBitsPerPixel(image),
                Q.CGImageGetBitsPerComponent(image), Q.CGImageGetBytesPerRow(image),
                int(Q.CGImageGetBitmapInfo(image)),
                Q.CGColorSpaceGetModel(Q.CGImageGetColorSpace(image)),
                hashlib.sha256(data).digest()))
        return all(value == fingerprints[0] for value in fingerprints[1:])
    except Exception:
        return False


def select_region(response, transcript_bounds):
    """First visible native result by position; interactivity is irrelevant."""
    candidates=[]
    for node in ax.nodes(response):
        el=node.element
        if (el.identifier or '').startswith('Identifier:ResultCell') and contains(transcript_bounds,el.bounds):
            title=next(iter(ax.extract_scoped_texts(node)),None)
            if title:
                candidates.append({'kind':'result-card-preview','bounds':el.bounds,'title':title})
        if el.identifier=='modelResponse-richTextCustomView':
            descendants=list(ax.nodes(node))
            has_cells=any((n.element.identifier or '').startswith('Identifier:ResultCell') for n in descendants)
            labels=ax.extract_scoped_texts(node)
            links=[n.element.label for n in descendants if n.element.role=='AXLink' and n.element.label]
            # A native embedded preview may be entirely static. A paragraph or
            # a lone progress label is not a preview. Never press a link.
            lines=[line.strip() for label in labels for line in label.splitlines() if line.strip()]
            if not has_cells and (links or len(set(lines))>=2) and contains(transcript_bounds,el.bounds):
                title=(links[0] if links else lines[0]).splitlines()[0]
                candidates.append({'kind':'note-preview' if links else 'embedded-result-preview',
                                   'bounds':el.bounds,'title':title})
    if candidates:
        return min(candidates,key=lambda region:(region['bounds'][1],region['bounds'][0]))
    raise PreviewNotReady('no fully visible usable current-result region')


def assert_unobscured(windows, pid, rect):
    """Find the unique on-screen Siri window containing the capture region.

    Capture uses CGWindowListCreateImage with this exact window ID, so
    overlapping windows cannot contribute pixels to the returned image.
    Still reject obscured targets as required by the capture contract.
    """
    targets=[]
    for i,w in enumerate(windows):
        b=w.get('kCGWindowBounds',{})
        bounds=tuple(b.get(k,0) for k in ('X','Y','Width','Height'))
        if w.get('kCGWindowOwnerPID')==pid and w.get('kCGWindowLayer')==0 and contains(bounds,rect):
            targets.append((i,w.get('kCGWindowNumber')))
    if len(targets)!=1:raise ValueError('capture window identity ambiguous')
    index, window_id = targets[0]
    obstruction=[]
    for other in windows[:index]:
        bounds = other.get('kCGWindowBounds', {})
        bounds = tuple(bounds.get(k, 0) for k in ('X', 'Y', 'Width', 'Height'))
        if other.get('kCGWindowAlpha', 1) > 0 and overlaps(bounds, rect):
            obstruction.append({'pid':other.get('kCGWindowOwnerPID'),
                'window_id':other.get('kCGWindowNumber'),'layer':other.get('kCGWindowLayer'),
                'alpha':other.get('kCGWindowAlpha',1),'bounds':bounds,
                'owner':other.get('kCGWindowOwnerName'),
                'sharing_state':other.get('kCGWindowSharingState')})
    # Full-screen Dock bookkeeping surfaces can have alpha=1 while contributing
    # no pixels. Keep every real/unknown overlay blocked; prove this region only.
    if obstruction:
        dock_only=all(o['owner']=='Dock' and o['layer']==20 for o in obstruction)
        if dock_only:
            transparent=dock_pixels_unobscured(obstruction,window_id,rect)
        else:
            # A protected/non-shareable surface may be omitted by screen
            # capture: equality cannot prove it transparent. Unknown state also
            # blocks. Read-only (1) surfaces are eligible for positive proof;
            # there is no application-name or layer exemption.
            transparent=(all(o['sharing_state']==1 for o in obstruction) and
                         region_pixels_equal(window_id,rect))
        if transparent:obstruction=[]
    if obstruction:
        raise CaptureObscured(window_id,rect,obstruction)
    return window_id


def snapshot_target(backend,pid,query,*,region_selector=None):
    pane,composer=backend._turn_container(pid)
    if composer.element.value:raise ValueError('composer edited; refusing capture')
    transcript=ax.transcript_container(pane)
    responses=[n for n in ax.nodes(transcript) if n.element.identifier=='modelResponse']
    prompts=[n for n in ax.nodes(transcript) if n.element.identifier=='userPrompt']
    if len(responses)>1 or len(prompts)!=1:raise ValueError('unique current turn required')
    prompt='\n'.join(ax.extract_scoped_texts(prompts[0]))
    if ' '.join(prompt.split())!=' '.join(query.split()):raise ValueError('prompt changed before capture')
    identity=tuple(n.ref for n in (pane,composer,transcript,prompts[0]))
    if not responses:
        return None,None,(*identity,None)
    response=responses[0]
    try:
        region=(region_selector or select_region)(response,transcript.element.bounds)
    except PreviewNotReady:
        region=None
    if region is not None and not contains(response.element.bounds,region['bounds']):
        raise ValueError('preview geometry escapes the current response')
    signature=hashlib.sha256(json.dumps([dataclasses.asdict(n.element) for n in ax.nodes(response)],sort_keys=True).encode()).hexdigest()
    extra=[]
    for path in getattr(region_selector,'identity_paths',()):
        node=response
        if region is not None:
            for index in path:node=node.children[index]
            extra.append(node.ref)
        else:extra.append(None)
    return region,signature,(*identity,response.ref,*extra)


def same_identity(before,after):
    """Bind the response when first created; never permit its replacement."""
    if len(before)<5 or len(before)!=len(after) or any(ref is None for ref in (*before[:4],*after[:4])):
        return False
    def equal(a,b):
        if a is b:return True
        from CoreFoundation import CFEqual
        return bool(CFEqual(a,b))
    return (all(equal(a,b) for a,b in zip(before[:4],after[:4])) and
            all(a is None or (b is not None and equal(a,b)) for a,b in zip(before[4:],after[4:])))


def capture_window_region(pid,window_id,rect,target):
    """Use WindowServer's single-window capture, excluding overlays/history."""
    import Quartz as Q
    from Foundation import NSURL
    x,y,w,h=rect
    cgrect=Q.CGRectMake(x,y,w,h)
    image=Q.CGWindowListCreateImage(cgrect,Q.kCGWindowListOptionIncludingWindow,
                                    window_id,Q.kCGWindowImageBoundsIgnoreFraming)
    if image is None:raise ValueError('WindowServer could not capture the Siri result region')
    if Q.CGImageGetWidth(image)<1 or Q.CGImageGetHeight(image)<1:
        raise ValueError('empty Siri result capture')
    url=NSURL.fileURLWithPath_(str(Path(target).resolve()))
    dest=Q.CGImageDestinationCreateWithURL(url,'public.png',1,None)
    if dest is None:raise ValueError('could not create a PNG result capture')
    Q.CGImageDestinationAddImage(dest,image,None)
    if not Q.CGImageDestinationFinalize(dest):raise ValueError('PNG capture could not be finalized')


def capture_current(backend,pid,query,target,*,deadline,pump=time.sleep,region_selector=None):
    """Capture a single Siri window region; never include other app windows.

    Returned metadata describes a diagnostic preview, never completion proof.
    """
    import Quartz as Q
    def owned():
        if time.monotonic()>=deadline:raise TimeoutError('capture deadline reached')
        if backend._find_pid()!=pid:raise ValueError('Siri process replaced')
        if time.monotonic()>=deadline:raise TimeoutError('capture deadline reached during identity read')
    def window(rect):
        windows=Q.CGWindowListCopyWindowInfo(Q.kCGWindowListOptionOnScreenOnly|Q.kCGWindowListExcludeDesktopElements,Q.kCGNullWindowID)
        return assert_unobscured(windows,pid,rect)
    target=Path(target)
    def snapshot():
        return (snapshot_target(backend,pid,query,region_selector=region_selector) if region_selector
                else snapshot_target(backend,pid,query))
    if target.exists():raise ValueError('refusing to overwrite capture evidence')
    identity=None
    attempts=0
    while True:
        owned()
        region,signature,current_identity=snapshot()
        if identity is None:
            if not same_identity(current_identity,current_identity):
                raise ValueError('current turn identity unavailable')
            identity=current_identity
        elif not same_identity(identity,current_identity):
            raise ValueError('current turn replaced; refusing capture')
        identity=current_identity
        if region is not None:break
        pump(min(.1,max(0,deadline-time.monotonic())))
    owned()
    window_id=window(region['bounds'])
    try:
        attempts+=1
        capture_window_region(pid,window_id,region['bounds'],target)
        owned();after,after_signature,after_identity=snapshot()
        if not same_identity(identity,after_identity):
            raise ValueError('current turn replaced during capture')
        if region!=after or signature!=after_signature or window(region['bounds'])!=window_id:
            raise ValueError('current result changed during capture')
        source=Q.CGImageSourceCreateWithURL(__import__('Foundation').NSURL.fileURLWithPath_(str(target.resolve())),None)
        image=Q.CGImageSourceCreateImageAtIndex(source,0,None) if source else None
        if not image or Q.CGImageGetWidth(image)<1 or Q.CGImageGetHeight(image)<1:
            raise ValueError('capture is not a decodable image')
        digest=hashlib.sha256(target.read_bytes()).hexdigest()
        owned()
        return {**region,'path':str(target),'sha256':digest,
                'window_id':window_id,'response_signature':signature,'completion':'unverified',
                'artifact_status':'captured','capture_attempts':attempts,
                'fresh_submission_verified':False}
    except Exception:
        if target.exists():
            target.rename(target.with_name(target.stem+'.rejected'+target.suffix))
        raise
