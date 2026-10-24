#!/usr/bin/env python3
"""Inspect ONLY a single current Siri turn. This is not completion proof.

Run with the installed PyObjC interpreter and PYTHONPATH=src.
--capture writes a fully visible native result-card or note preview only.
No card is pressed, no file is opened, and no query is submitted here.
"""
import argparse
import json
import fcntl
import time
import uuid
from pathlib import Path
from siri_cli.driver.base import SiriError
from siri_cli.driver.siriai import SiriAiBackend
from siri_cli.capture.current import capture_current
from siri_cli.capture.deadline import bounded_ax_reads


def collect_evidence(backend,pid,query,target,*,deadline):
    """Separate scoped extraction, artifact delivery and completion uncertainty."""
    report={'inspection_id':str(uuid.uuid4()),'query':query,
            'readback_status':'not_read','artifact_status':'not_requested',
            'completion':'unverified','fresh_submission_verified':False,
            'accepted_transaction':False}
    try:
        report['state']=backend.inspect_current_turn(pid,expected_query=query)
        report['readback_status']='extracted' if report['state'].get('text') else 'no_answer'
    except (SiriError,ValueError,TimeoutError) as exc:
        report.update(readback_status='failed',error=str(exc),error_type=type(exc).__name__)
        return report
    if target:
        try:
            report['artifact']=capture_current(backend,pid,query,target,deadline=deadline)
            report['artifact_status']='captured'
        except (SiriError,ValueError,TimeoutError,OSError) as exc:
            report.update(artifact_status='failed',error=str(exc),error_type=type(exc).__name__)
            if hasattr(exc,'details'):report['capture_error_details']=exc.details
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-query', required=True)
    parser.add_argument('--capture', type=Path)
    parser.add_argument('--report',type=Path,help='Save extraction and capture outcomes separately; never a successful transaction')
    args = parser.parse_args()
    lock = open('/tmp/siri-cli-current-turn.lock', 'a+')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.exit(1, 'another Siri transaction owns the lock\n')
    backend = SiriAiBackend()
    pid = backend._find_pid()
    if not pid:
        parser.exit(1, 'Siri AI is not running\n')
    started=time.monotonic()
    try:
        with bounded_ax_reads(started+120):
            report=collect_evidence(backend,pid,args.expected_query,args.capture,deadline=started+120)
        report['elapsed_s']=round(time.monotonic()-started,4)
        if args.report:
            with args.report.open('x') as output:json.dump(report,output,indent=2)
        if report.get('error'):parser.exit(1,report['error']+'\n')
    finally:
        lock.close()
    state=report['state']
    if 'artifact' in report:state['artifact']=report['artifact']
    print(json.dumps(state, indent=2))


if __name__ == '__main__':
    main()
