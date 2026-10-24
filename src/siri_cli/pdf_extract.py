"""Quarantine one copied PDF. No Siri, OCR, clipboard polling or relay startup.

Only extract_after_copy() reads a supplied port. Extraction is not source
attribution, human approval or native generation completion.
"""
from dataclasses import dataclass
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import stat
import subprocess
import sys
import urllib.parse
import uuid
import warnings

PDF_TYPES=('com.adobe.pdf','Apple PDF pasteboard type')
FILE_URL_TYPE='public.file-url'
MAX_BYTES=20_000_000

class PDFExtractionError(Exception):pass


def validator_interpreter():
    """Use an existing parser runtime; never install or access the network."""
    import importlib.util
    if importlib.util.find_spec('pypdf') is not None:return sys.executable
    raise PDFExtractionError('pdf_validator_unavailable_set_validator_python')

@dataclass(frozen=True)
class QuarantinedPDF:
    path: Path
    size: int
    sha256: str
    review_required: bool = True
    source_attribution: str = 'unverified'
    completion: str = 'unverified'

class MacPasteboard:
    """Explicit live bridge; importing/constructing this class reads no clipboard."""
    def _board(self):
        from AppKit import NSPasteboard
        return NSPasteboard.generalPasteboard()
    def change_count(self):return int(self._board().changeCount())
    def items(self):return list(self._board().pasteboardItems() or [])
    def types(self,item):return tuple(str(t) for t in item.types() or [])
    def data(self,item,kind):
        value=item.dataForType_(kind)
        return None if value is None else bytes(value)


def _parse_pdf(data):
    # Child-only validation. Parser warnings must never disclose PDF contents.
    logging.disable(logging.CRITICAL);warnings.simplefilter('ignore')
    try:from pypdf import PdfReader
    except ImportError:return 'pdf_validator_unavailable'
    try:
        if not data.startswith(b'%PDF-'):return 'invalid_pdf'
        reader=PdfReader(io.BytesIO(data),strict=True)
        if reader.is_encrypted:return 'encrypted_pdf_unsupported'
        if reader.trailer['/Root']['/Type']!='/Catalog':return 'invalid_pdf'
        count=len(reader.pages)
        if not 0<count<=2000:return 'invalid_pdf'
        for page in reader.pages:
            if page['/Type']!='/Page':return 'invalid_pdf'
            box=page.mediabox
            if box.width<=0 or box.height<=0:return 'invalid_pdf'
            contents=page.get_contents()
            if contents is not None:contents.get_data()
        return 'valid_pdf'
    except Exception:return 'invalid_pdf'


def validate_pdf(data,*,python=None,timeout=5):
    try:
        result=subprocess.run([python or sys.executable,'-m','siri_cli.pdf_extract','--validate'],
            input=data,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=timeout)
    except subprocess.TimeoutExpired:raise PDFExtractionError('pdf_validation_timeout') from None
    except OSError:raise PDFExtractionError('pdf_validator_unavailable') from None
    try:status=json.loads(result.stdout)['status']
    except (ValueError,KeyError,TypeError):raise PDFExtractionError('pdf_validation_failed') from None
    if result.returncode or status!='valid_pdf':
        allowed={'invalid_pdf','encrypted_pdf_unsupported','pdf_validator_unavailable'}
        raise PDFExtractionError(status if status in allowed else 'pdf_validation_failed')


def _file_bytes(value,limit):
    try:
        url=value.decode('utf-8','strict');parts=urllib.parse.urlsplit(url)
        if parts.scheme!='file' or parts.netloc not in ('','localhost') or parts.query or parts.fragment:
            raise PDFExtractionError('unsupported_file_url')
        # Reject malformed percent escapes instead of repairing the locator.
        import re
        if re.search(r'%(?![0-9a-fA-F]{2})',parts.path):raise PDFExtractionError('invalid_file_url')
        path=os.fsdecode(urllib.parse.unquote_to_bytes(parts.path))
        if not path.startswith('/') or '\0' in path:raise PDFExtractionError('invalid_file_url')
        before=os.lstat(path)
        if not stat.S_ISREG(before.st_mode):raise PDFExtractionError('not_regular_file')
        if getattr(before,'st_flags',0)&getattr(stat,'SF_DATALESS',0x40000000):raise PDFExtractionError('file_not_local')
        if before.st_size>limit:raise PDFExtractionError('pdf_oversized')
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            opened=os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or (before.st_dev,before.st_ino)!=(opened.st_dev,opened.st_ino):raise PDFExtractionError('file_changed')
            data=bytearray()
            while len(data)<=limit:
                chunk=os.read(fd,min(65536,limit+1-len(data)))
                if not chunk:break
                data.extend(chunk)
            after=os.fstat(fd)
            if (opened.st_size,opened.st_mtime_ns)!=(after.st_size,after.st_mtime_ns) or len(data)!=after.st_size:raise PDFExtractionError('file_changed')
        finally:os.close(fd)
        return bytes(data)
    except PDFExtractionError:raise
    except (OSError,UnicodeError,ValueError):raise PDFExtractionError('file_unavailable') from None


def _quarantine(folder,data):
    folder=Path(folder)
    try:folder.mkdir(mode=0o700)
    except FileExistsError:pass
    info=folder.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode&0o077 or info.st_uid!=os.getuid():raise PDFExtractionError('quarantine_not_private')
    directory=os.open(folder,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    name=uuid.uuid4().hex+'.pdf';created=False
    try:
        fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory);created=True
        with os.fdopen(fd,'wb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
    except BaseException:
        if created:os.unlink(name,dir_fd=directory)
        raise
    finally:os.close(directory)
    return folder/name


def quarantine_pdf_bytes(data,folder,*,python=None,max_bytes=MAX_BYTES,log=lambda event:None):
    """Validate an explicit export's bytes; never pretend it was a clipboard copy."""
    status='extraction_failed';size=len(data) if isinstance(data,bytes) else 0
    try:
        if not isinstance(data,bytes) or not data:raise PDFExtractionError('invalid_pdf')
        if size>max_bytes:raise PDFExtractionError('pdf_oversized')
        validate_pdf(data,python=python)
        path=_quarantine(folder,data);status='quarantined_review_required'
        return QuarantinedPDF(path,size,hashlib.sha256(data).hexdigest())
    except PDFExtractionError as error:status=str(error);raise
    finally:log({'types':['com.adobe.pdf'],'sizes':[size],'status':status})


def extract_after_copy(port,before_count,folder,*,python=None,max_bytes=MAX_BYTES,log=lambda event:None):
    """Require one observed clipboard write; this does not identify its writer.

    Caller must bind the document UI separately. Unknown flavors are never read.
    Conflicting representations fail rather than guessing which is the document.
    """
    types=[];sizes=[];status='extraction_failed'
    try:
        count=port.change_count()
        if count-before_count!=1:raise PDFExtractionError('clipboard_change_unverified')
        items=port.items()
        if len(items)!=1:raise PDFExtractionError('clipboard_items_ambiguous')
        item=items[0];types=list(port.types(item))
        supported=[t for t in types if t in PDF_TYPES or t==FILE_URL_TYPE]
        if not supported:raise PDFExtractionError('pdf_representation_missing')
        payloads=[]
        for kind in supported:
            raw=port.data(item,kind)
            if not isinstance(raw,bytes) or not raw:raise PDFExtractionError('clipboard_data_missing')
            if len(raw)>max_bytes:raise PDFExtractionError('pdf_oversized')
            data=_file_bytes(raw,max_bytes) if kind==FILE_URL_TYPE else raw
            sizes.append(len(data))
            if not data or len(data)>max_bytes:raise PDFExtractionError('pdf_oversized' if data else 'invalid_pdf')
            payloads.append(data)
        if any(data!=payloads[0] for data in payloads[1:]):raise PDFExtractionError('pdf_representations_conflict')
        validate_pdf(payloads[0],python=python)
        if port.change_count()!=count:raise PDFExtractionError('clipboard_changed_during_read')
        path=_quarantine(folder,payloads[0]);status='quarantined_review_required'
        return QuarantinedPDF(path,len(payloads[0]),hashlib.sha256(payloads[0]).hexdigest())
    except PDFExtractionError as error:status=str(error);raise
    except Exception:raise PDFExtractionError('extraction_failed') from None
    finally:log({'types':types,'sizes':sizes,'status':status})

if __name__=='__main__':
    if sys.argv[1:]!=['--validate']:raise SystemExit(2)
    data=sys.stdin.buffer.read(MAX_BYTES+1)
    status=_parse_pdf(data) if len(data)<=MAX_BYTES else 'invalid_pdf'
    print(json.dumps({'status':status}))
    raise SystemExit(0 if status=='valid_pdf' else 1)
