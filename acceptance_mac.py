"""Bounded local acceptance. Unverified candidate captures cannot pass.

Run only during an explicitly coordinated Mac-control window. No retry.
"""
import json
import fcntl
import sys
import time
from pathlib import Path
from siri_cli.driver.siriai import SiriAiBackend
from siri_cli.driver.base import SiriError
from siri_cli.capture.current import capture_current


def acceptance_passed(result):
    artifact=Path(result.get('capture',''))
    return bool(result.get('ask_success') and result.get('completion_verified')
                and result.get('artifact_verified') and artifact.is_file()
                and artifact.stat().st_size>0)


def _run(run):
    query='show me exam 3 study guide'
    backend=SiriAiBackend(foreground=True)
    start=time.monotonic()
    result={'query':query,'run':run,'ask_success':False,
            'completion_verified':False,'artifact_verified':False}
    try:
        response=backend.ask(query,timeout_s=120)
        result.update(ask_success=True,text=response.text)
        # A future validated detector must supply explicit per-run evidence.
        evidence=getattr(response,'completion_evidence',None)
        result['completion_verified']=bool(evidence and evidence.get('verified') is True)
    except SiriError as exc:
        result['error']=str(exc)
    result['elapsed_s']=round(time.monotonic()-start,3)
    # Never rescue a failed/unverified ask through a separate extraction path.
    if result['completion_verified']:
        try:
            target=Path(f'evidence/query-{run}-card.png')
            result['artifact']=capture_current(backend,backend._find_pid(),query,
                                               target,deadline=start+120)
            result['capture']=str(target)
            result['artifact_verified']=True
        except (SiriError,ValueError,TimeoutError) as exc:
            result['artifact_error']=str(exc)
    result['accepted']=acceptance_passed(result)
    Path(f'evidence/query-{run}.json').write_text(json.dumps(result,indent=2))
    print(json.dumps({k:v for k,v in result.items() if k!='candidate'},indent=2),flush=True)
    return 0 if result['accepted'] else 1


def main():
    with open('/tmp/siri-cli-current-turn.lock', 'a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('another Siri transaction owns the lock', file=sys.stderr)
            return 1
        return _run(sys.argv[1])


if __name__=='__main__':
    raise SystemExit(main())
