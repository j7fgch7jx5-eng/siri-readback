"""Rendered-response classification, never native generation completion."""
from .capture import ax,current
import math
import re


class SiriReportedError(ValueError):
    def __init__(self,message):
        self.source_message=message
        super().__init__('siri_reported_error')


def response_structure(response):
    rows=[]
    def walk(node,path=()):
        if len(rows)>=128:return
        identifier=node.element.identifier or ''
        rows.append({'path':list(path),'role':node.element.role,
            'identifier':identifier if identifier.startswith('modelResponse') else ('ResultCell' if identifier.startswith('Identifier:ResultCell') else None),
            'bounds':node.element.bounds,'child_count':len(node.children),'actions':list(node.element.actions)})
        for i,child in enumerate(node.children):walk(child,path+(i,))
    walk(response)
    return {'complete':response.complete,'nodes':rows}


def _rectangle_status(rect):
    if rect is None:return 'missing'
    if not isinstance(rect,(tuple,list)) or len(rect)!=4:return 'malformed'
    if any(not isinstance(v,(int,float)) or not math.isfinite(v) for v in rect):return 'non_finite_or_nonnumeric'
    if rect[2]<=0 or rect[3]<=0:return 'nonpositive_extent'
    return 'valid'


class DocumentBoundsError(ValueError):
    """Content-free geometry evidence; rejection is still mandatory."""
    def __init__(self,transcript,response,document):
        super().__init__('document_bounds_rejected')
        rects={'transcript':transcript,'response':response,'document':document}
        status={name:_rectangle_status(rect) for name,rect in rects.items()}
        checks={name:status[name]=='valid' and status['document']=='valid' and current.contains(rect,document)
                for name,rect in rects.items() if name!='document'}
        # JSON-safe finite numeric rectangles only; no labels or file paths.
        self.geometry={'coordinate_space':'AX_screen_points',
            'rectangles':{name:list(rect) if status[name] in ('valid','nonpositive_extent') else None for name,rect in rects.items()},
            'rectangle_status':status,'contains_document':checks,
            'failed_containment':[name for name,passed in checks.items() if not passed]}


def document_bounds_failure(error):
    seen=set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error,DocumentBoundsError):return error.geometry
        error=error.__cause__
    return None


def select_document(response,transcript_bounds):
    cells=[n for n in ax.nodes(response) if (n.element.identifier or '').startswith('Identifier:ResultCell')]
    if not cells:
        from .photo_preview import select_single_photo
        return select_single_photo(response,transcript_bounds)
    if not response.complete or len(cells)!=1:raise ValueError('ambiguous_document_cards')
    cell=cells[0];titles=ax.extract_scoped_texts(cell)
    if not any(t.casefold().endswith('.pdf') for t in titles):raise ValueError('document_type_unverified')
    if 'AXPress' not in cell.element.actions:raise ValueError('document_not_actionable')
    if any(n.element.role=='AXButton' and ax._path_to(cell,n) is None for n in ax.nodes(response)):raise ValueError('mixed_document_response')
    rects=(transcript_bounds,response.element.bounds,cell.element.bounds)
    if any(_rectangle_status(rect)!='valid' for rect in rects) or not current.contains(transcript_bounds,cell.element.bounds) or not current.contains(response.element.bounds,cell.element.bounds):
        raise DocumentBoundsError(*rects)
    return {'kind':'pdf-document','bounds':cell.element.bounds,'title':next(t for t in titles if t.casefold().endswith('.pdf'))}


def select_readable(response,transcript_bounds):
    if not response.complete:raise ValueError('incomplete_response')
    # Observed native text layout: the source-attribution placeholder collapses
    # to zero height beside the paragraph. Only that demonstrably empty leaf
    # is auxiliary; positive-height, labeled, nested or unread nodes still wait.
    content=[]
    for node in response.children:
        e=node.element;r=e.bounds
        empty_attribution=(e.role=='AXUnknown' and e.identifier=='modelResponse-richTextSourceAttribution'
            and not node.children and not any((e.value,e.description,e.title,e.url,e.filename,e.document))
            and isinstance(r,(tuple,list)) and len(r)==4
            and all(isinstance(v,(int,float)) and math.isfinite(v) for v in r)
            and r[2]>=0 and r[3]==0 and all(
                outer and outer[0]<=r[0] and outer[1]<=r[1] and
                r[0]+r[2]<=outer[0]+outer[2] and r[1]<=outer[1]+outer[3]
                for outer in (response.element.bounds,transcript_bounds)))
        if not empty_attribution:content.append(node)
    # Exact standalone error observed in the live document run. Do not treat
    # this source failure as an answer or wait for file bytes that never arrive.
    from .runtime_policy import source_error
    if (len(content)==1 and content[0].element.identifier=='modelResponse-richTextParagraph'
        and not content[0].children and source_error(content[0].element.text)):
        raise SiriReportedError(content[0].element.text)
    if any(n.element.role=='AXProgressIndicator' for n in ax.nodes(response)):
        raise current.PreviewNotReady('progress_indicator_present')
    calculator=calculator_text_region(response,transcript_bounds)
    if calculator is not None:return calculator
    sourced=text_with_sources(response,transcript_bounds)
    if sourced is not None:return sourced
    views=[n for n in ax.nodes(response) if n.element.identifier=='modelResponse-richTextCustomView']
    if views and all(not view.children for view in views):
        raise current.PreviewNotReady('empty_custom_view_pending')
    if any((n.element.identifier or '').startswith('Identifier:ResultCell') or n.element.identifier=='modelResponse-richTextCustomView' for n in ax.nodes(response)):
        return select_document(response,transcript_bounds)
    if not content:raise current.PreviewNotReady('no_rendered_text')
    if not all(n.element.role=='AXStaticText' and n.element.identifier=='modelResponse-richTextParagraph' and not n.children for n in content):
        raise current.PreviewNotReady('unsupported_text_structure')
    if not all(current.contains(transcript_bounds,n.element.bounds) and current.contains(response.element.bounds,n.element.bounds) for n in content):raise ValueError('text_bounds_rejected')
    text='\n'.join(n.element.text for n in content if n.element.text)
    if not text:raise current.PreviewNotReady('empty_text')
    return {'kind':'text-candidate','bounds':response.element.bounds,'title':'Current rendered text','text':text}


def text_with_sources(response,transcript_bounds):
    """Observed cloud answer: paragraphs, collapsed placeholder, citation controls.

    Citation controls are exposed as metadata, not rewritten into answer text.
    A nonempty or visible custom view remains outside this text branch.
    """
    paragraphs=[];sources=[];empty_views=[]
    for node in response.children:
        e=node.element
        if e.role=='AXStaticText' and e.identifier=='modelResponse-richTextParagraph' and not node.children:
            paragraphs.append(node)
        elif e.role=='AXButton' and e.identifier=='modelResponse-richTextSourceAttribution' and not node.children and 'AXPress' in e.actions:
            sources.append(node)
        elif e.role=='AXGroup' and e.identifier=='modelResponse-richTextCustomView' and not node.children and not any((e.value,e.title,e.description,e.url,e.filename,e.document)):
            r=e.bounds
            if not r or len(r)!=4 or not all(isinstance(v,(int,float)) and math.isfinite(v) for v in r) or r[2]<=0 or r[3]!=0:return None
            if not all(outer and outer[0]<=r[0] and r[0]+r[2]<=outer[0]+outer[2] and outer[1]<=r[1]<=outer[1]+outer[3] for outer in (response.element.bounds,transcript_bounds)):return None
            empty_views.append(node)
        else:return None
    if not paragraphs or not 1<=len(sources)<=4 or len(empty_views)>1:return None
    if not all(current.contains(response.element.bounds,n.element.bounds) and current.contains(transcript_bounds,n.element.bounds) for n in paragraphs+sources):raise ValueError('text_source_bounds_rejected')
    if not all(0<n.element.bounds[3]<=32 for n in sources):raise ValueError('text_source_control_unverified')
    text='\n'.join(n.element.text for n in paragraphs if n.element.text)
    if not text:raise current.PreviewNotReady('empty_text')
    from .runtime_policy import source_error
    if source_error(text):raise SiriReportedError(text)
    return {'kind':'text-candidate','subtype':'text-with-source-controls','bounds':response.element.bounds,'title':'Current text with sources',
            'text':text,'sources':[{'label':n.element.text,'url':n.element.url} for n in sources]}


def calculator_text_region(response,transcript_bounds):
    """Observed numeric calculator card. File labels/other widgets do not qualify."""
    if len(response.children)!=2:return None
    paragraph,view=response.children
    if paragraph.element.identifier!='modelResponse-richTextParagraph' or paragraph.element.role!='AXStaticText' or paragraph.children or view.element.identifier!='modelResponse-richTextCustomView':return None
    cells=[n for n in ax.nodes(view) if (n.element.identifier or '').startswith('Identifier:ResultCell')]
    if len(cells)!=1 or len(cells[0].children)!=1:return None
    cell=cells[0];group=cell.children[0]
    if group.element.role!='AXGroup' or len(group.children)!=3:return None
    expression,value,button=group.children
    if ([n.element.role for n in group.children]!=['AXStaticText','AXStaticText','AXButton'] or
        any(n.children for n in group.children) or button.element.text!='Copy'):return None
    lhs=expression.element.text or '';number=value.element.text or ''
    if not re.fullmatch(r'[0-9.,+\-−×*/÷^() eE]+=',lhs) or not any(c.isdigit() for c in lhs):return None
    if not re.fullmatch(r'[+\-−]?(?:\d[\d,]*(?:\.\d+)?|\.\d+)(?:[eE][+\-]?\d+)?',number):return None
    if any(n.element.role not in ('AXGroup','AXScrollArea','AXList','AXCell','AXStaticText','AXButton') for n in ax.nodes(view)):return None
    rects=[paragraph.element.bounds,cell.element.bounds,*[n.element.bounds for n in group.children]]
    if not all(current.contains(response.element.bounds,r) and current.contains(transcript_bounds,r) for r in rects):raise ValueError('calculator_text_bounds_rejected')
    if not all(current.contains(cell.element.bounds,n.element.bounds) for n in group.children):raise ValueError('calculator_children_clipped')
    x=min(r[0] for r in rects);y=min(r[1] for r in rects);right=max(r[0]+r[2] for r in rects);bottom=max(r[1]+r[3] for r in rects)
    text='\n'.join(s for s in (paragraph.element.text,lhs,number) if s)
    return {'kind':'text-candidate','subtype':'calculator-card','bounds':(x,y,right-x,bottom-y),'title':'Current calculator text','text':text}


def read_bound_text(ticket,port,identity):
    """Two complete reads of the same bound node; this is consistency, not done."""
    from .capture.transaction import _same_ref
    import dataclasses,hashlib,json
    results=[]
    for _ in range(2):
        ticket.check()
        b=port.backend
        observed_pid=b._find_pid();ticket.check()
        if observed_pid!=identity[0]:raise ValueError('text_process_replaced')
        windows=b._windows(ax.app_for_pid(identity[0]))
        if len(windows)!=1 or not _same_ref(windows[0],identity[1]):raise ValueError('text_window_replaced')
        root=ax.snapshot_tree(windows[0],structural=True);composer=ax.find_composer_node(root)
        pane=ax.conversation_container(root,composer);transcript=ax.transcript_container(pane)
        ps=[n for n in ax.nodes(transcript) if n.element.identifier=='userPrompt'];rs=[n for n in ax.nodes(transcript) if n.element.identifier=='modelResponse']
        if not root.complete or len(ps)!=1 or len(rs)!=1:raise ValueError('text_scope_incomplete_or_ambiguous')
        refs=(windows[0],pane.ref,composer.ref,transcript.ref,ps[0].ref,rs[0].ref)
        same={name:_same_ref(a,b) for name,a,b in zip(
            ('window','pane','composer','transcript','prompt','response'),refs,identity[1:])}
        if not all(same.values()):
            event={'phase':'text_identity_replaced','same':same,'read_index':len(results)}
            if hasattr(port,'emit'):port.emit(event)
            error=ValueError('text_identity_replaced');error.identity_failure=event
            raise error
        if ax._get_attr_checked(composer.ref,'AXValue'):raise ValueError('text_composer_edited')
        prompt='\n'.join(ax.extract_scoped_texts(ax.snapshot_tree(identity[5])))
        if prompt!=ticket.incoming.query:raise ValueError('text_prompt_changed')
        response=ax.snapshot_tree(identity[6])
        region=select_readable(response,ax._element_bounds(transcript.ref,reader=ax._get_attr_checked))
        if region['kind']!='text-candidate':raise ValueError('response_kind_changed')
        signature=hashlib.sha256(json.dumps([dataclasses.asdict(n.element) for n in ax.nodes(response)],sort_keys=True).encode()).hexdigest()
        text=region.get('text','\n'.join(n.element.text for n in response.children if n.element.text))
        results.append((signature,text))
    if results[0]!=results[1]:raise ValueError('rendered_text_changed')
    port.text_sources=region.get('sources',[])
    return results[0][1]
