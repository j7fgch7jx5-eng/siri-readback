"""Verify and display an existing local result. Never submits or sends."""
import argparse,json,sys
from pathlib import Path
from .local_readback import RunRecord
from .relay import RelayFailure
from .runtime_policy import FAILURE_MESSAGE,PENDING_MESSAGE


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('run_directory');p.add_argument('--json',action='store_true')
    a=p.parse_args(argv)
    try:
        directory=Path(a.run_directory).absolute();path=directory/'result.json'
        if path.is_symlink():raise ValueError('run_record_is_symlink')
        saved=json.loads(path.read_text())
        record=RunRecord(directory,saved['run_id'],saved['query'],saved['mode']=='existing',saved.get('expect','auto'))
        result=record.resume()
        if result is None:raise ValueError('result_not_available')
        if a.json:print(json.dumps(result,ensure_ascii=False,indent=2))
        else:
            print(f"Run: {result['run_id']}\nQuery: {result['query']}\nKind: {result['kind']}\nCompletion: unverified\n")
            print('Exact Siri text:')
            sys.stdout.write(result['text']);print('\n')
            if result.get('sources'):
                print('Siri citation controls (separate from exact answer text):')
                for source in result['sources']:print(json.dumps(source,ensure_ascii=False))
            if result['kind']=='document':
                print('Validated PDF: '+result['pdf'])
                print('SHA-256: '+result['sha256'])
                print('Open this PDF and inspect the actual drawing before any separate send.')
            print('Human review required. Nothing submitted, staged or sent.')
        return 0
    except (OSError,ValueError,KeyError,RuntimeError,RelayFailure) as error:
        pending=str(error) in ('previous_request_still_pending','run_already_attempted_no_auto_retry_pending')
        message=PENDING_MESSAGE if pending else FAILURE_MESSAGE
        if a.json:print(json.dumps({'status':'pending' if pending else 'failed','message':message,'reason':str(error),'sent':False,'messages_staged':False}),file=sys.stderr)
        else:print(message,file=sys.stderr)
        return 2 if pending else 1


if __name__=='__main__':raise SystemExit(main())
