"""Capture-only experiment. Production overlap policy is deliberately unchanged."""
import argparse
import dataclasses
import fcntl
import hashlib
import json
from pathlib import Path
import time
import unicodedata
from siri_cli.capture import ax, current
from siri_cli.capture.deadline import bounded_ax_reads
from siri_cli.driver.siriai import SiriAiBackend

REQUESTED_PROMPT="what's weather"
ALLOWED_PROMPTS=(REQUESTED_PROMPT,'what’s weather')  # Explicit diagnostic-only alias.
EXPECTED_LABELS=['Lehigh Acres','66°','Partly Cloudy','H:82° L:65°']


def describe_prompt(value,segments=None):
    """Only the structurally selected prompt; no cleanup or normalization."""
    return {'observed_prompt':value,'repr':repr(value),
            'unicode_escape':value.encode('unicode_escape').decode('ascii'),
            'length':len(value),'segments':segments if segments is not None else [value],
            'characters':[{'index':i,'codepoint':f'U+{ord(c):04X}',
                'name':unicodedata.name(c,'UNNAMED'),'whitespace':c.isspace()}
                for i,c in enumerate(value)]}


class PromptMismatch(ValueError):
    def __init__(self,value):
        super().__init__('existing prompt is not an approved exact spelling')
        self.prompt_observation=describe_prompt(value)


def exact_prompt(value):
    if value not in ALLOWED_PROMPTS:raise PromptMismatch(value)
    return value


def structural_pair(transcript):
    """Observed provider layout only. Never infer a pair from screen position."""
    if not transcript.complete:raise ValueError('incomplete transcript structure')
    def walk(node,path=()):
        yield path,node
        for i,child in enumerate(node.children):yield from walk(child,path+(i,))
    entries=dict(walk(transcript))
    prompts=[(p,n) for p,n in entries.items() if n.element.identifier=='userPrompt']
    if len(prompts)!=1:raise ValueError('unique current prompt required')
    prompt_path,prompt=prompts[0]
    providers=[(p,n) for p,n in entries.items() if n.element.role=='AXOpaqueProviderGroup'
               and len(p)<len(prompt_path) and prompt_path[:len(p)]==p]
    if len(providers)!=1:raise ValueError('unknown prompt/provider layout')
    provider_path,provider=providers[0];slot=prompt_path[len(provider_path)]
    if slot+1>=len(provider.children):raise ValueError('no response block immediately follows prompt')
    following=provider_path+(slot+1,)
    responses=[(p,n) for p,n in entries.items() if n.element.identifier=='modelResponse']
    selected=[(p,n) for p,n in responses if p[:len(following)]==following]
    if len(selected)!=1:raise ValueError('ambiguous response block following prompt')
    chosen_path,response=selected[0]
    for p,node in responses:
        if p==chosen_path:continue
        if p[:len(provider_path)]!=provider_path or len(p)<=len(provider_path) or p[len(provider_path)]>=slot:
            raise ValueError('unpaired response is not strictly before current prompt')
    return prompt,response,{'prompt_path':prompt_path,'response_path':chosen_path,
                            'ignored_prior_response_count':len(responses)-1}


def read_target(backend,pid,*,on_prompt=None):
    windows=backend._windows(ax.app_for_pid(pid))
    if len(windows)!=1:raise ValueError('unique Siri AX window required')
    # The old _turn_container() expands every response's content. This path
    # locates the pair structurally before requesting any prompt/response text.
    root=ax.snapshot_tree(windows[0],structural=True)
    composer=ax.find_composer_node(root)
    if composer is None:raise ValueError('unique composer required')
    pane=ax.conversation_container(root,composer)
    if pane is None:raise ValueError('unique current pane required')
    transcript=ax.transcript_container(pane)
    prompt,response,pair=structural_pair(transcript)
    if ax._get_attr_checked(composer.ref,'AXValue'):raise ValueError('composer is not blank')
    scoped_prompt=ax.snapshot_tree(prompt.ref)
    segments=ax.extract_scoped_texts(scoped_prompt)
    observation=describe_prompt('\n'.join(segments),segments)
    observation.update(scope='selected userPrompt only',complete=scoped_prompt.complete,
                       prompt_path=pair['prompt_path'])
    if on_prompt is not None:on_prompt(observation)
    if not scoped_prompt.complete:raise ValueError('incomplete scoped prompt read')
    actual=exact_prompt(observation['observed_prompt'])
    scoped_response=ax.snapshot_tree(response.ref)
    if not scoped_response.complete:raise ValueError('incomplete selected response')
    transcript_bounds=ax._element_bounds(transcript.ref,reader=ax._get_attr_checked)
    region=current.select_region(scoped_response,transcript_bounds)
    if not current.contains(scoped_response.element.bounds,region['bounds']):
        raise ValueError('preview escapes selected current response')
    signature=hashlib.sha256(json.dumps([dataclasses.asdict(n.element) for n in ax.nodes(scoped_response)],sort_keys=True).encode()).hexdigest()
    identity=tuple(n.ref for n in (pane,composer,transcript,prompt,response))
    cards=[{'bounds':n.element.bounds,'labels':ax.extract_scoped_texts(n)} for n in ax.nodes(scoped_response)
           if (n.element.identifier or '').startswith('Identifier:ResultCell')]
    state={'text':'\n'.join(ax.extract_scoped_texts(scoped_response)),'cards':cards,'completion':'unverified'}
    cards=[card for card in cards if tuple(card['bounds'] or ())==tuple(region['bounds'])]
    labels=[line.strip() for line in cards[0]['labels']] if len(cards)==1 else []
    return {'window_ref':windows[0],'identity':identity,'region':region,'signature':signature,
            'actual_prompt':actual,'state':state,'labels':labels,'pair':pair}


def window_context(quartz,pid,rect):
    windows=quartz.CGWindowListCopyWindowInfo(
        quartz.kCGWindowListOptionOnScreenOnly|quartz.kCGWindowListExcludeDesktopElements,quartz.kCGNullWindowID)
    try:
        window_id=current.assert_unobscured(windows,pid,rect)
        return {'window_id':window_id,'overlapping_windows':[]}
    except current.CaptureObscured as exc:
        # Only this diagnostic renders the identified window despite overlap.
        # No production guard or allowlist is changed.
        return exc.details


def capture_experiment(backend,pid,target,*,deadline,quartz=None):
    if quartz is None:import Quartz as quartz
    report={'requested_prompt':REQUESTED_PROMPT,'completion':'unverified',
            'fresh_submission_verified':False,'accepted_transaction':False,
            'render_mode':'diagnostic-isolated-window','image_status':'not_captured',
            'visual_readability':'not_reviewed','prompt_observations':[],
            'capture_attempted':False}
    target=Path(target)
    if target.exists():raise ValueError('refusing to overwrite evidence')
    def owned():
        if backend._find_pid()!=pid:raise ValueError('Siri process replaced')
        if time.monotonic()>=deadline:raise TimeoutError('isolated capture deadline expired')
    try:
        owned();before=read_target(backend,pid,on_prompt=lambda o:report['prompt_observations'].append({'phase':'before',**o}))
        report.update(actual_prompt=before['actual_prompt'],
            alias_used=before['actual_prompt']!=REQUESTED_PROMPT,
            region=before['region'],state=before['state'],actual_labels=before['labels'])
        if 'pair' in before:report['structural_pair']=before['pair']
        if before['labels']!=EXPECTED_LABELS:raise ValueError('existing weather result differs from saved intended result')
        report['before']=window_context(quartz,pid,before['region']['bounds'])
        owned()
        report['capture_attempted']=True
        current.capture_window_region(pid,report['before']['window_id'],before['region']['bounds'],target)
        owned();after=read_target(backend,pid,on_prompt=lambda o:report['prompt_observations'].append({'phase':'after',**o}))
        from CoreFoundation import CFEqual
        if (not CFEqual(before['window_ref'],after['window_ref']) or
            not current.same_identity(before['identity'],after['identity']) or
            any(before[key]!=after[key] for key in ('region','signature','actual_prompt','labels'))):
            raise ValueError('current weather target changed during isolated capture')
        report['after']=window_context(quartz,pid,after['region']['bounds'])
        if report['before']['window_id']!=report['after']['window_id']:
            raise ValueError('Siri capture window replaced')
        from Foundation import NSURL
        source=quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(str(target.resolve())),None)
        image=quartz.CGImageSourceCreateImageAtIndex(source,0,None) if source else None
        if not image:raise ValueError('isolated capture cannot be decoded')
        size=(quartz.CGImageGetWidth(image),quartz.CGImageGetHeight(image))
        if min(size)<=0:raise ValueError('empty isolated capture')
        report.update(pixel_size=size,sha256=hashlib.sha256(target.read_bytes()).hexdigest())
        owned();report.update(image_status='captured_pending_visual_review',path=str(target))
    except Exception as exc:
        report.update(error=str(exc),error_type=type(exc).__name__,image_status='failed')
        if hasattr(exc,'prompt_observation') and not report['prompt_observations']:
            report['prompt_observations'].append(exc.prompt_observation)
        if target.exists():
            rejected=target.with_name(target.stem+'.rejected'+target.suffix)
            target.rename(rejected);report['rejected_path']=str(rejected)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args();args.output_dir.mkdir(parents=True,exist_ok=False)
    started=time.monotonic();deadline=started+120
    with open('/tmp/siri-cli-current-turn.lock','a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        backend=SiriAiBackend()
        with bounded_ax_reads(deadline):
            report=capture_experiment(backend,backend._find_pid(),args.output_dir/'weather-preview.png',deadline=deadline)
        report['elapsed_s']=time.monotonic()-started
        (args.output_dir/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k!='state'},indent=2))
    return 1 if 'error' in report else 0


if __name__=='__main__':raise SystemExit(main())
