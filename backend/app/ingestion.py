from dataclasses import dataclass, asdict
from pathlib import Path
import csv, json, re
import fitz
from docx import Document as DocxDocument
from pptx import Presentation
import openpyxl

@dataclass
class EvidenceNode:
    id: str
    document_id: str
    source_name: str
    modality: str
    text: str
    page: int | None = None
    slide: int | None = None
    sheet: str | None = None
    logical_document_id: str | None = None
    metadata: dict | None = None

@dataclass
class Chunk:
    id: str
    evidence_id: str
    text: str
    document_id: str
    source_name: str
    page: int | None
    metadata: dict

SUPPORTED = {
    '.pdf':'pdf','.docx':'docx','.pptx':'pptx','.xlsx':'xlsx','.csv':'csv',
    '.json':'json','.txt':'text','.md':'text','.markdown':'text','.log':'text',
    '.png':'image','.jpg':'image','.jpeg':'image','.webp':'image','.tif':'image','.tiff':'image',
    '.mp3':'audio','.wav':'audio','.m4a':'audio','.mp4':'audio','.aac':'audio','.flac':'audio','.ogg':'audio','.webm':'audio',
}


def detect_type(path: Path) -> str:
    return SUPPORTED.get(path.suffix.lower(), 'unknown')


def _logical_pdf_segments(pages: list[str]):
    # Conservative heuristic only: explicit page markers and strong title-like starts.
    # It never claims a segment boundary is certain; metadata records the heuristic.
    segments=[]; start=1; current=[]
    for i, text in enumerate(pages, 1):
        current.append(i)
        if i < len(pages):
            nxt = pages[i].strip()
            boundary = bool(re.search(r'(?im)^(?:document|agreement|invoice|statement|passport|visa|application|form)\b', nxt))
            if boundary and len(current) > 1:
                segments.append(current); current=[]; start=i+1
    if current: segments.append(current)
    return segments


def extract_document(path: Path, document_id: str) -> tuple[str, list[EvidenceNode], dict]:
    kind=detect_type(path); nodes=[]; meta={'detected_type':kind,'specialist_required':False}
    if kind=='pdf':
        with fitz.open(path) as pdf:
            texts=[p.get_text('text').strip() for p in pdf]
            segments=_logical_pdf_segments(texts)
            for pno,text in enumerate(texts,1):
                if text:
                    seg=next((str(j) for j,s in enumerate(segments,1) if pno in s), '1')
                    nodes.append(EvidenceNode(f'{document_id}:p{pno}',document_id,path.name,'text',text,page=pno,logical_document_id=f'{document_id}:logical:{seg}',metadata={'extraction':'native','logical_segment_heuristic':True}))
                else:
                    meta['specialist_required']=True
                    nodes.append(EvidenceNode(f'{document_id}:p{pno}',document_id,path.name,'page', '', page=pno,metadata={'extraction':'empty_native_text','requires_ocr':True}))
        meta['pages']=len(texts); meta['logical_documents']=len(segments)
        return kind,nodes,meta
    if kind=='docx':
        d=DocxDocument(path); text='\n'.join(p.text for p in d.paragraphs if p.text.strip())
        nodes.append(EvidenceNode(f'{document_id}:docx',document_id,path.name,'text',text,metadata={'extraction':'native'})); return kind,nodes,meta
    if kind=='pptx':
        prs=Presentation(path)
        for i,slide in enumerate(prs.slides,1):
            text='\n'.join(sh.text for sh in slide.shapes if hasattr(sh,'text') and sh.text.strip())
            nodes.append(EvidenceNode(f'{document_id}:s{i}',document_id,path.name,'text',text,slide=i,metadata={'extraction':'native'}))
        meta['slides']=len(prs.slides); return kind,nodes,meta
    if kind=='xlsx':
        wb=openpyxl.load_workbook(path, read_only=True, data_only=True)
        for ws in wb.worksheets:
            rows=[]
            for row in ws.iter_rows(values_only=True): rows.append('\t'.join('' if v is None else str(v) for v in row))
            text='\n'.join(rows).strip()
            nodes.append(EvidenceNode(f'{document_id}:sheet:{ws.title}',document_id,path.name,'table',text,sheet=ws.title,metadata={'extraction':'native'}))
        return kind,nodes,meta
    if kind=='csv':
        with path.open('r',encoding='utf-8-sig',errors='replace',newline='') as f: text='\n'.join('\t'.join(r) for r in csv.reader(f))
        nodes.append(EvidenceNode(f'{document_id}:csv',document_id,path.name,'table',text,metadata={'extraction':'native'})); return kind,nodes,meta
    if kind=='json':
        obj=json.loads(path.read_text(encoding='utf-8',errors='replace'))
        nodes.append(EvidenceNode(f'{document_id}:json',document_id,path.name,'structured',json.dumps(obj,ensure_ascii=False,indent=2),metadata={'extraction':'native'})); return kind,nodes,meta
    if kind=='text':
        nodes.append(EvidenceNode(f'{document_id}:text',document_id,path.name,'text',path.read_text(encoding='utf-8',errors='replace'),metadata={'extraction':'native'})); return kind,nodes,meta
    if kind in {'image','audio'}:
        meta['specialist_required']=True
        nodes.append(EvidenceNode(f'{document_id}:pending',document_id,path.name,kind,'',metadata={'extraction':'specialist_required'})); return kind,nodes,meta
    meta['specialist_required']=True
    nodes.append(EvidenceNode(f'{document_id}:unknown',document_id,path.name,'unknown','',metadata={'extraction':'unsupported'})); return kind,nodes,meta


def chunk_nodes(nodes: list[EvidenceNode], chunk_size=1200, overlap=180) -> list[Chunk]:
    out=[]
    for n in nodes:
        if not n.text.strip(): continue
        text=re.sub(r'\n{3,}','\n\n',n.text).strip()
        start=0; idx=0
        while start < len(text):
            end=min(len(text),start+chunk_size)
            if end < len(text):
                cut=max(text.rfind('\n',start,end),text.rfind('. ',start,end),text.rfind(' ',start,end))
                if cut > start+chunk_size//2: end=cut+1
            part=text[start:end].strip()
            if part:
                out.append(Chunk(f'{n.id}:c{idx}',n.id,part,n.document_id,n.source_name,n.page,{'modality':n.modality,'slide':n.slide,'sheet':n.sheet,'logical_document_id':n.logical_document_id,'start_char':start,'end_char':end}))
            if end>=len(text): break
            start=max(end-overlap,start+1); idx+=1
    return out
