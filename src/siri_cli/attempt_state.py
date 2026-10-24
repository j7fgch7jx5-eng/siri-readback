"""Persistent no-overlap boundary after an uncertain submitted request."""
import json,os
from pathlib import Path
from .relay import RelayFailure


class AttemptLease:
    def __init__(self,root):
        self.root=Path(root);self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        if self.root.is_symlink() or self.root.stat().st_uid!=os.getuid() or self.root.stat().st_mode&0o077:
            raise RelayFailure('attempt_state_not_private')
        self.path=self.root/'active.json';self.owned=False
    def begin(self,record):
        from .local_readback import atomic_json
        if self.path.is_symlink():raise RelayFailure('attempt_state_is_symlink')
        if self.path.exists():
            old=json.loads(self.path.read_text())
            if old.get('status') in ('active','pending','uncertain'):
                raise RelayFailure('previous_request_still_pending')
        atomic_json(self.path,{'status':'active','run_id':record.run_id,'run_directory':str(record.directory)})
        self.owned=True
    def finish(self,result):
        if not self.owned:return
        from .local_readback import atomic_json
        attempted=bool(result.get('submission_attempts'))
        conclusive=(result.get('status')=='readable_preview' or result.get('reason')=='siri_reported_error' or result.get('conclusive_failure') or not attempted)
        if conclusive:
            atomic_json(self.path,{'status':'resolved','run_id':result.get('run_id'),'outcome':result.get('status')})
        else:
            result.update(status='pending',message='Siri is still pending. This request was not retried.',response_uncertain=True)
            atomic_json(self.path,{'status':'pending','run_id':result.get('run_id'),'run_directory':str(result.get('run_directory','')),'reason':result.get('reason')})
