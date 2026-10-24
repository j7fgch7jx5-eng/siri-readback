"""Allowlisted provider failure metadata. Never export arbitrary exception text."""
import re
from siri_cli.capture.ax import ax_read_failure
from siri_cli.driver.base import CaptureError
from siri_cli.relay import RelayFailure


def provider_failure_details(error):
    detail=ax_read_failure(error)
    reason=None
    seen=set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error,RelayFailure) and re.fullmatch(r'[a-z][a-z0-9_]{0,95}',str(error)):
            reason=str(error)
        if detail is None and isinstance(error,CaptureError):
            match=re.fullmatch(r'AX application (children|child role) read failed \((-?\d+)\)',str(error))
            if match:
                children=match[1]=='children'
                detail={'operation':'AXUIElementCopyAttributeValue',
                    'attribute':'AXChildren' if children else 'AXRole',
                    'error_code':int(match[2]),'structural_path':[] if children else None,
                    'path_origin':'application_root',
                    'node_attributes_required_for_path':False}
        error=error.__cause__
    return {'failure_reason':reason,'ax_failure':detail}
