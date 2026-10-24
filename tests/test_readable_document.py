import io,json,subprocess,sys,time
from pathlib import Path
from unittest.mock import patch
import pytest
from pypdf import PdfReader,PdfWriter
from siri_cli.pdf_extract import PDFExtractionError,PDF_TYPES,FILE_URL_TYPE
from siri_cli.readable_document import extract_readable_after_copy,IMAGE_TYPES
from test_pdf_extract import Port,pdf

@pytest.fixture(scope='module')
def images():
 code="""import Quartz as Q,Foundation as F,sys
ctx=Q.CGBitmapContextCreate(None,64,48,8,64*4,Q.CGColorSpaceCreateDeviceRGB(),Q.kCGImageAlphaNoneSkipLast)
Q.CGContextSetRGBFillColor(ctx,.8,.2,.1,1);Q.CGContextFillRect(ctx,Q.CGRectMake(0,0,64,48))
im=Q.CGBitmapContextCreateImage(ctx);data=F.NSMutableData.data();d=Q.CGImageDestinationCreateWithData(data,sys.argv[1],1,None)
Q.CGImageDestinationAddImage(d,im,None)
if not Q.CGImageDestinationFinalize(d):raise SystemExit(2)
sys.stdout.buffer.write(bytes(data))
"""
 result={}
 for kind in IMAGE_TYPES:
  p=subprocess.run([sys.executable,'-c',code,kind],capture_output=True,timeout=15)
  assert p.returncode==0 and p.stdout,'Native fixture encoder unavailable: '+kind
  result[kind]=p.stdout
 return result

def extract(tmp_path,item):
 logs=[];p=Port([item]);a=extract_readable_after_copy(p,1,tmp_path/'q',log=logs.append)
 return a,logs

@pytest.mark.parametrize('kind',IMAGE_TYPES)
@pytest.mark.parametrize('file_url',[False,True])
def test_real_image_decode_and_pdf_conversion_preserve_review_gates(tmp_path,images,kind,file_url):
 data=images[kind];item={kind:data}
 if file_url:
  f=tmp_path/'no-extension';f.write_bytes(data);item={FILE_URL_TYPE:f.as_uri().encode()}
 artifact,logs=extract(tmp_path,item);reader=PdfReader(artifact.path,strict=True)
 assert len(reader.pages)==1 and len(reader.pages[0].images)==1
 assert reader.pages[0].images[0].image.size==(64,48)
 assert artifact.source_type==kind and artifact.converted and artifact.review_required
 assert artifact.completion=='unverified' and not artifact.fresh_submission_verified
 assert artifact.path.stat().st_mode&0o777==0o600 and artifact.path.parent.stat().st_mode&0o777==0o700
 assert all(set(e)=={'types','sizes','status'} for e in logs)
 assert 'no-extension' not in json.dumps(logs)


def test_pdf_path_still_preserves_original_pdf_bytes(tmp_path):
 data=pdf();a,_=extract(tmp_path,{PDF_TYPES[0]:data})
 assert a.path.read_bytes()==data and not a.converted and a.source_type=='com.adobe.pdf'

@pytest.mark.parametrize('kind',IMAGE_TYPES)
def test_false_image_type_or_partial_data_fails_closed(tmp_path,kind):
 with pytest.raises(PDFExtractionError):extract(tmp_path,{kind:b'not a decodable image'})
 assert not (tmp_path/'q').exists()

def test_mismatched_declared_format_rejected(tmp_path,images):
 with pytest.raises(PDFExtractionError,match='declared_type_mismatch'):extract(tmp_path,{'public.jpeg':images['public.png']})

def test_conflicting_file_and_native_representation_not_guessed(tmp_path,images):
 f=tmp_path/'source';f.write_bytes(images['public.heic'])
 with pytest.raises(PDFExtractionError,match='document_representations_conflict'):
  extract(tmp_path,{FILE_URL_TYPE:f.as_uri().encode(),'public.png':images['public.png']})

def test_stale_clipboard_does_not_read_data(tmp_path):
 p=Port([{'public.png':b'ignored'}],count=1)
 with pytest.raises(PDFExtractionError,match='clipboard_change_unverified'):extract_readable_after_copy(p,1,tmp_path/'q')
 assert p.reads==[]

def test_multiple_items_rejected(tmp_path):
 p=Port([{'public.png':b'a'},{'public.png':b'b'}])
 with pytest.raises(PDFExtractionError,match='clipboard_items_ambiguous'):extract_readable_after_copy(p,1,tmp_path/'q')

def test_oversized_input_rejected_before_conversion(tmp_path):
 with patch('siri_cli.readable_document.MAX_BYTES',10),pytest.raises(PDFExtractionError,match='document_oversized'):
  extract(tmp_path,{'public.png':b'x'*11})

def test_deadline_rejects_without_a_deliverable(tmp_path,images):
 with pytest.raises(PDFExtractionError,match='document_deadline'):
  extract_readable_after_copy(Port([{'public.png':images['public.png']}]),1,tmp_path/'q',deadline=time.monotonic()-1)
 assert not (tmp_path/'q').exists()

def test_conversion_timeout_reaps_without_pdf(tmp_path,images):
 with patch('siri_cli.readable_document.subprocess.run',side_effect=subprocess.TimeoutExpired('image',15)),pytest.raises(PDFExtractionError,match='image_conversion_timeout'):
  extract(tmp_path,{'public.png':images['public.png']})

def test_mutating_clipboard_during_validation_rejected(tmp_path,images):
 p=Port([{'public.png':images['public.png']}])
 def validate(*args,**kw):p.count+=1
 with patch('siri_cli.readable_document.validate_pdf',validate),pytest.raises(PDFExtractionError,match='clipboard_changed_during_read'):
  extract_readable_after_copy(p,1,tmp_path/'q')
 assert not (tmp_path/'q').exists()
