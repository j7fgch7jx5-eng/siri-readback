"""One copied PDF or image -> reviewed local PDF. No network, OCR or sender."""
from dataclasses import dataclass
import hashlib,json,subprocess,sys
from .pdf_extract import (PDFExtractionError,PDF_TYPES,FILE_URL_TYPE,MAX_BYTES,
                         _file_bytes,_quarantine,validate_pdf)
IMAGE_TYPES=('public.heic','public.jpeg','public.png')


def _pdf_with_jpeg(jpeg,width,height):
    scale=792/max(width,height);w=width*scale;h=height*scale
    commands=f'q\n{w:.6f} 0 0 {h:.6f} 0 0 cm\n/Im0 Do\nQ\n'.encode()
    objects=[b'<< /Type /Catalog /Pages 2 0 R >>',b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
      f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w:.6f} {h:.6f}] /Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>'.encode(),
      f'<< /Type /XObject /Subtype /Image /Width {width} /Height {height} /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length {len(jpeg)} >>\nstream\n'.encode()+jpeg+b'\nendstream',
      f'<< /Length {len(commands)} >>\nstream\n'.encode()+commands+b'endstream']
    data=bytearray(b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n');offsets=[0]
    for i,obj in enumerate(objects,1):
        offsets.append(len(data));data.extend(f'{i} 0 obj\n'.encode()+obj+b'\nendobj\n')
    start=len(data);data.extend(f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode())
    for offset in offsets[1:]:data.extend(f'{offset:010d} 00000 n \n'.encode())
    data.extend(f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n'.encode())
    return bytes(data)


def _convert_image(data):
    import Quartz as Q
    import Foundation as F
    source=Q.CGImageSourceCreateWithData(F.NSData.dataWithBytes_length_(data,len(data)),None)
    if source is None:raise ValueError('image_decode_failed')
    kind=str(Q.CGImageSourceGetType(source))
    if kind not in IMAGE_TYPES:raise ValueError('unsupported_image_type')
    if Q.CGImageSourceGetCount(source)!=1:raise ValueError('multiple_images_unsupported')
    prop=Q.CGImageSourceCopyPropertiesAtIndex(source,0,None)
    width=int(prop.get(Q.kCGImagePropertyPixelWidth,0));height=int(prop.get(Q.kCGImagePropertyPixelHeight,0))
    if not width or not height or width*height>50_000_000:raise ValueError('image_dimensions_rejected')
    options={Q.kCGImageSourceCreateThumbnailFromImageAlways:True,
             Q.kCGImageSourceCreateThumbnailWithTransform:True,
             Q.kCGImageSourceThumbnailMaxPixelSize:max(width,height),
             Q.kCGImageSourceShouldCacheImmediately:True}
    image=Q.CGImageSourceCreateThumbnailAtIndex(source,0,options)
    if image is None:raise ValueError('image_decode_failed')
    pixels=Q.CGDataProviderCopyData(Q.CGImageGetDataProvider(image))
    if pixels is None or not len(pixels):raise ValueError('image_decode_failed')
    w=int(Q.CGImageGetWidth(image));h=int(Q.CGImageGetHeight(image))
    # Apply native orientation without a screenshot or OCR. Preserve resolution.
    if sorted((w,h))!=sorted((width,height)):raise ValueError('image_dimensions_changed')
    output=F.NSMutableData.data();destination=Q.CGImageDestinationCreateWithData(output,'public.jpeg',1,None)
    if destination is None:raise ValueError('image_encoder_unavailable')
    Q.CGImageDestinationAddImage(destination,image,{Q.kCGImageDestinationLossyCompressionQuality:.95})
    if not Q.CGImageDestinationFinalize(destination):raise ValueError('image_encoding_failed')
    pdf=_pdf_with_jpeg(bytes(output),w,h)
    if len(pdf)>MAX_BYTES:raise ValueError('pdf_oversized')
    return {'type':kind,'width':w,'height':h,'converted':True},pdf


@dataclass(frozen=True)
class ReadableDocument:
    path: object
    size: int
    sha256: str
    source_type: str
    converted: bool
    review_required: bool = True
    completion: str = 'unverified'
    fresh_submission_verified: bool = False


def extract_readable_after_copy(port,before_count,folder,*,validator_python=None,
                                image_python=None,deadline=None,log=lambda e:None):
    import time
    def remaining(cap):
        value=cap if deadline is None else min(cap,deadline-time.monotonic())
        if value<=0:raise PDFExtractionError('document_deadline')
        return value
    types=[];sizes=[];status='extraction_failed'
    try:
        count=port.change_count()
        if count-before_count!=1:raise PDFExtractionError('clipboard_change_unverified')
        items=port.items()
        if len(items)!=1:raise PDFExtractionError('clipboard_items_ambiguous')
        item=items[0];types=list(port.types(item));payloads=[];declared=[]
        for kind in types:
            if kind not in (*PDF_TYPES,*IMAGE_TYPES,FILE_URL_TYPE):continue
            raw=port.data(item,kind)
            if not isinstance(raw,bytes) or not raw:raise PDFExtractionError('clipboard_data_missing')
            if len(raw)>MAX_BYTES:raise PDFExtractionError('document_oversized')
            data=_file_bytes(raw,MAX_BYTES) if kind==FILE_URL_TYPE else raw
            if not data or len(data)>MAX_BYTES:raise PDFExtractionError('document_oversized')
            payloads.append(data);declared.append(kind);sizes.append(len(data))
        if not payloads:raise PDFExtractionError('document_representation_missing')
        if any(p!=payloads[0] for p in payloads[1:]):raise PDFExtractionError('document_representations_conflict')
        data=payloads[0]
        if data.startswith(b'%PDF-'):
            if any(t in IMAGE_TYPES for t in declared):raise PDFExtractionError('declared_type_mismatch')
            info={'type':'com.adobe.pdf','converted':False};pdf=data
        else:
            if any(t in PDF_TYPES for t in declared):raise PDFExtractionError('declared_type_mismatch')
            try:
                result=subprocess.run([image_python or sys.executable,'-m','siri_cli.readable_document','--convert-image'],input=data,
                   stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=remaining(15))
            except subprocess.TimeoutExpired:raise PDFExtractionError('image_conversion_timeout') from None
            except OSError:raise PDFExtractionError('image_converter_unavailable') from None
            try:header,pdf=result.stdout.split(b'\n',1);info=json.loads(header)
            except Exception:raise PDFExtractionError('image_conversion_failed') from None
            if result.returncode or info.get('type') not in IMAGE_TYPES:raise PDFExtractionError('image_conversion_failed')
            if any(t in IMAGE_TYPES and t!=info['type'] for t in declared):raise PDFExtractionError('declared_type_mismatch')
        if len(pdf)>MAX_BYTES:raise PDFExtractionError('pdf_oversized')
        validate_pdf(pdf,python=validator_python,timeout=remaining(5))
        if port.change_count()!=count:raise PDFExtractionError('clipboard_changed_during_read')
        path=_quarantine(folder,pdf);status='quarantined_review_required'
        return ReadableDocument(path,len(pdf),hashlib.sha256(pdf).hexdigest(),info['type'],bool(info['converted']))
    except PDFExtractionError as e:status=str(e);raise
    except Exception:raise PDFExtractionError('document_extraction_failed') from None
    finally:log({'types':types,'sizes':sizes,'status':status})

if __name__=='__main__':
    if sys.argv[1:]!=['--convert-image']:raise SystemExit(2)
    try:
        data=sys.stdin.buffer.read(MAX_BYTES+1)
        if len(data)>MAX_BYTES:raise ValueError('document_oversized')
        info,pdf=_convert_image(data)
        print(json.dumps(info),flush=True);sys.stdout.buffer.write(pdf)
    except Exception:
        print(json.dumps({'status':'image_conversion_failed'}),flush=True);raise SystemExit(1)
