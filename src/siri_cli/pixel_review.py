"""Conservative automatic OCR of the actual captured pixels, not AX alone."""
import hashlib
import re
import json
import subprocess
import sys
import time
from .relay import RelayFailure,validate_png


def vision_lines(path):
    import Foundation as F
    import Vision as V
    request=V.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(V.VNRequestTextRecognitionLevelAccurate)
    handler=V.VNImageRequestHandler.alloc().initWithURL_options_(F.NSURL.fileURLWithPath_(str(path)),F.NSDictionary.dictionary())
    ok,error=handler.performRequests_error_([request],None)
    if not ok or error:raise RelayFailure('pixel_ocr_failed')
    return [(str(x.string()),float(x.confidence())) for result in request.results() for x in result.topCandidates_(1)]


class PixelReviewer:
    def __init__(self,ocr=vision_lines):self.ocr=ocr
    def __call__(self,path,ticket,expected_title):
        ticket.check();data=path.read_bytes();validate_png(data)
        if self.ocr is vision_lines:
            # Bound native Vision in a child process; OCR cannot hold the Siri
            # execution lease indefinitely after the request deadline.
            result=subprocess.run([sys.executable,'-c',
                'import json,sys;from siri_cli.pixel_review import vision_lines;print(json.dumps(vision_lines(sys.argv[1])))',str(path)],
                capture_output=True,text=True,timeout=max(.001,min(10,ticket.deadline-time.monotonic())))
            if result.returncode:raise RelayFailure('pixel_ocr_unavailable')
            lines=json.loads(result.stdout)
        else:lines=self.ocr(path)
        ticket.check()
        readable=' '.join(text for text,confidence in lines if confidence>=.8)
        words=lambda s:re.findall(r'\w+',s.casefold())
        expected=words(expected_title);actual=words(readable)
        # Require visible native title plus additional legible content. This is
        # preview legibility evidence, never completeness or semantic truth.
        if len(expected)<1 or len(actual)<4 or not all(w in actual for w in expected):
            raise RelayFailure('preview_pixels_not_readable_or_title_mismatch')
        if path.read_bytes()!=data:raise RelayFailure('attachment_changed')
        return hashlib.sha256(data).hexdigest()
