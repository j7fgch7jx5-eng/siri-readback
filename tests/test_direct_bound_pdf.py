import hashlib,io,threading,time
from types import SimpleNamespace as N
from pathlib import Path
from unittest.mock import patch
import pytest
from reportlab.pdfgen.canvas import Canvas
from siri_cli.document_response import read_bound_document
from siri_cli.relay import Ticket,Incoming,RelayFailure
from siri_cli.pdf_extract import PDFExtractionError


def pdf_bytes():
 out=io.BytesIO();c=Canvas(out);c.drawString(40,750,'Synthetic document fixture');c.showPage();c.save();return out.getvalue()


@pytest.mark.parametrize('mode',['ok','missing','not_pdf','ambiguous','cancel','source_changed'])
def test_real_parser_direct_current_response_payload(tmp_path,mode):
 source=tmp_path/'source.pdf'
 data=b'not a PDF' if mode=='not_pdf' else pdf_bytes()
 if mode!='missing':source.write_bytes(data)
 run=tmp_path/'run';run.mkdir(mode=0o700)
 cancel=threading.Event()
 if mode=='cancel':cancel.set()
 ticket=Ticket('bound-run',Incoming('request','local-only','diagram'),run,time.monotonic()+5,cancel)
 calls=[]
 class UI:
  def verify(self,bound):
   calls.append('verify')
   if mode=='source_changed' and len(calls)==3:raise RelayFailure('source_changed')
  def open_document(self,bound):pytest.fail('exposed URL should not open a card')
  def audit(self,event):pass
 bound={'region':{'kind':'pdf-document'},'payload_urls':[source.as_uri()]}
 if mode=='ambiguous':bound['payload_urls'].append((tmp_path/'different.pdf').as_uri())
 if mode=='ok':
  artifact=read_bound_document(ticket,UI(),bound)
  assert artifact.path.read_bytes()==data and artifact.sha256==hashlib.sha256(data).hexdigest()
  assert artifact.converted is False and artifact.source_type=='public.file-url'
  assert artifact.path.stat().st_mode&0o077==0 and artifact.path.parent.stat().st_mode&0o077==0
 else:
  with pytest.raises((RelayFailure,PDFExtractionError)):read_bound_document(ticket,UI(),bound)
  assert not list((run/'quarantine').glob('*.pdf'))
