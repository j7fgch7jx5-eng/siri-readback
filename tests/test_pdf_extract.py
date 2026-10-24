import io,json,os,subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from pypdf import PdfWriter
from siri_cli.pdf_extract import extract_after_copy,PDFExtractionError,PDF_TYPES,FILE_URL_TYPE,validate_pdf

def pdf(encrypted=False):
    w=PdfWriter();w.add_blank_page(width=200,height=100)
    if encrypted:w.encrypt('fixture-password')
    b=io.BytesIO();w.write(b);return b.getvalue()

class Port:
    def __init__(self,items,count=2):self.rows=items;self.count=count;self.reads=[]
    def change_count(self):return self.count
    def items(self):return self.rows
    def types(self,item):return tuple(item)
    def data(self,item,kind):self.reads.append(kind);return item[kind]

def extract(tmp_path,rows,**kw):
    logs=[];p=Port(rows)
    artifact=extract_after_copy(p,1,tmp_path/'private',log=logs.append,**kw)
    return artifact,p,logs

@pytest.mark.parametrize('kind',PDF_TYPES)
def test_native_pdf_data_validated_and_private_generated_name(tmp_path,kind):
    data=pdf();a,p,logs=extract(tmp_path,[{kind:data,'public.utf8-plain-text':b'private source label'}])
    assert a.path.read_bytes()==data and a.path.suffix=='.pdf'
    assert len(a.path.stem)==32 and a.path.parent.stat().st_mode&0o777==0o700
    assert a.path.stat().st_mode&0o777==0o600
    assert a.review_required and a.completion=='unverified' and a.source_attribution=='unverified'
    assert p.reads==[kind] and set(logs[0])=={'types','sizes','status'}
    assert 'private source label' not in json.dumps(logs)

def test_file_url_accepts_valid_pdf_without_pdf_extension_and_percent_encoding(tmp_path):
    f=tmp_path/'private document ü.bin';f.write_bytes(pdf())
    a,p,logs=extract(tmp_path,[{FILE_URL_TYPE:f.as_uri().encode()}])
    assert a.path.read_bytes()==f.read_bytes() and f.name not in a.path.name
    assert f.name not in json.dumps(logs) and str(tmp_path) not in json.dumps(logs)

def test_consistent_url_and_native_aliases_accept_one_document(tmp_path):
    f=tmp_path/'document';data=pdf();f.write_bytes(data)
    a,_,_=extract(tmp_path,[{FILE_URL_TYPE:f.as_uri().encode(),PDF_TYPES[0]:data,PDF_TYPES[1]:data}])
    assert a.path.read_bytes()==data

@pytest.mark.parametrize('rows',[[],[{PDF_TYPES[0]:b'x'},{PDF_TYPES[0]:b'y'}]])
def test_multiple_or_zero_items_rejected(tmp_path,rows):
    with pytest.raises(PDFExtractionError,match='clipboard_items_ambiguous'):extract(tmp_path,rows)
    assert not (tmp_path/'private').exists()

@pytest.mark.parametrize('data',[b'not a PDF',b'%PDF-1.7\nnot an actual PDF\n%%EOF',b'\x89PNG\r\n',b''])
def test_actual_content_not_extension_or_magic_is_required(tmp_path,data):
    f=tmp_path/'convincing.pdf';f.write_bytes(data)
    with pytest.raises(PDFExtractionError,match='invalid_pdf'):extract(tmp_path,[{FILE_URL_TYPE:f.as_uri().encode()}])
    assert not (tmp_path/'private').exists()

def test_missing_file_even_when_native_flavor_present_is_rejected(tmp_path):
    with pytest.raises(PDFExtractionError,match='file_unavailable'):
        extract(tmp_path,[{FILE_URL_TYPE:(tmp_path/'missing.pdf').as_uri().encode(),PDF_TYPES[0]:pdf()}])

def test_conflicting_representations_rejected(tmp_path):
    f=tmp_path/'source.pdf';f.write_bytes(pdf())
    w=PdfWriter();w.add_blank_page(width=300,height=200);b=io.BytesIO();w.write(b)
    with pytest.raises(PDFExtractionError,match='pdf_representations_conflict'):
        extract(tmp_path,[{FILE_URL_TYPE:f.as_uri().encode(),PDF_TYPES[0]:b.getvalue()}])

@pytest.mark.parametrize('is_url',[False,True])
def test_oversized_payloads_rejected(tmp_path,is_url):
    data=pdf();f=tmp_path/'large.pdf';f.write_bytes(data)
    rows=[{FILE_URL_TYPE:f.as_uri().encode()}] if is_url else [{PDF_TYPES[0]:data}]
    with pytest.raises(PDFExtractionError,match='pdf_oversized'):extract(tmp_path,rows,max_bytes=100)

@pytest.mark.parametrize('url',[b'https://example.invalid/file.pdf',b'file://remote-host/secret.pdf',b'file:///tmp/file.pdf?x=1',b'file:///tmp/file%00.pdf',b'file:///tmp/bad%ZZ.pdf'])
def test_unsupported_or_malformed_urls_never_fetch(tmp_path,url):
    with pytest.raises(PDFExtractionError):extract(tmp_path,[{FILE_URL_TYPE:url}])

def test_symlink_and_directory_rejected(tmp_path):
    f=tmp_path/'real.pdf';f.write_bytes(pdf());link=tmp_path/'link.pdf';link.symlink_to(f)
    for p in [link,tmp_path]:
        with pytest.raises(PDFExtractionError,match='not_regular_file'):extract(tmp_path,[{FILE_URL_TYPE:p.as_uri().encode()}])

def test_stale_or_multiple_clipboard_writes_rejected_before_data_reads(tmp_path):
    for count in [1,3]:
        p=Port([{PDF_TYPES[0]:pdf()}],count)
        with pytest.raises(PDFExtractionError,match='clipboard_change_unverified'):extract_after_copy(p,1,tmp_path/'private')
        assert p.reads==[]

def test_clipboard_change_during_read_rejected(tmp_path):
    p=Port([{PDF_TYPES[0]:pdf()}]);original=p.data
    def read(*args):p.count+=1;return original(*args)
    p.data=read
    with pytest.raises(PDFExtractionError,match='clipboard_changed_during_read'):extract_after_copy(p,1,tmp_path/'private')
    assert not (tmp_path/'private').exists()

def test_existing_nonprivate_quarantine_not_modified(tmp_path):
    folder=tmp_path/'private';folder.mkdir(mode=0o755)
    with pytest.raises(PDFExtractionError,match='quarantine_not_private'):extract(tmp_path,[{PDF_TYPES[0]:pdf()}])
    assert folder.stat().st_mode&0o777==0o755 and list(folder.iterdir())==[]

def test_unknown_flavors_not_read_and_no_pdf_not_accepted(tmp_path):
    p=Port([{'public.tiff':b'image data'}]);logs=[]
    with pytest.raises(PDFExtractionError,match='pdf_representation_missing'):extract_after_copy(p,1,tmp_path/'private',log=logs.append)
    assert p.reads==[] and logs==[{'types':['public.tiff'],'sizes':[],'status':'pdf_representation_missing'}]

def test_encrypted_pdf_rejected(tmp_path):
    with pytest.raises(PDFExtractionError,match='encrypted_pdf_unsupported'):extract(tmp_path,[{PDF_TYPES[0]:pdf(True)}])

def test_validation_timeout_fails_closed():
    with patch('siri_cli.pdf_extract.subprocess.run',side_effect=subprocess.TimeoutExpired('fixture',5)),pytest.raises(PDFExtractionError,match='pdf_validation_timeout'):validate_pdf(pdf())

def test_validator_nonthrowing_failure_is_not_accepted():
    for output in [b'not json',b'{"status":"valid_pdf"}']:
        with patch('siri_cli.pdf_extract.subprocess.run',return_value=SimpleNamespace(returncode=1,stdout=output)),pytest.raises(PDFExtractionError):validate_pdf(pdf())

def test_explicit_export_validation_is_not_a_clipboard_receipt(tmp_path):
    from siri_cli.pdf_extract import quarantine_pdf_bytes
    data=pdf();logs=[];a=quarantine_pdf_bytes(data,tmp_path/'private',log=logs.append)
    assert a.path.read_bytes()==data and a.source_attribution=='unverified' and a.review_required
    assert logs==[{'types':['com.adobe.pdf'],'sizes':[len(data)],'status':'quarantined_review_required'}]

@pytest.mark.parametrize('data',[b'not a PDF',b'x'*101])
def test_invalid_or_oversized_explicit_export_rejected(tmp_path,data):
    from siri_cli.pdf_extract import quarantine_pdf_bytes
    with pytest.raises(PDFExtractionError):quarantine_pdf_bytes(data,tmp_path/'private',max_bytes=100)
    assert not (tmp_path/'private').exists()
