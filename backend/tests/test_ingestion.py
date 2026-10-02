from pathlib import Path
import fitz
from pathlib import Path

from app.ingestion import detect_type, chunk_nodes, EvidenceNode, extract_document
from app.multimodal.assemblyai import TranscriptResult, TranscriptSegment
from app.multimodal.audio_evidence import transcript_to_evidence
from app.multimodal.image_evidence import ocr_to_evidence
from app.multimodal.ocr import OCRDetection

def test_detect_types():
    assert detect_type(Path('a.pdf')) == 'pdf'
    assert detect_type(Path('a.jpg')) == 'image'
    assert detect_type(Path('a.wav')) == 'audio'

def test_chunking_preserves_source():
    n=EvidenceNode('d:p1','d','a.pdf','text','hello world '*200,page=1)
    chunks=chunk_nodes([n],chunk_size=100,overlap=10)
    assert len(chunks)>1
    assert all(c.document_id=='d' and c.page==1 for c in chunks)

def test_pdf_extraction_preserves_page_metadata():
    path = Path("data") / "pytest_retention.pdf"
    path.parent.mkdir(exist_ok=True)
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((72, 72), "Company data must be retained for 90 days.")
    pdf.save(path)
    pdf.close()
    try:
        kind, nodes, meta = extract_document(path, "doc-1")
        assert kind == "pdf" and meta["pages"] == 1
        assert nodes[0].page == 1 and "90 days" in nodes[0].text
    finally:
        path.unlink(missing_ok=True)

def test_ocr_and_audio_evidence_preserve_metadata():
    ocr = ocr_to_evidence("doc-1", [OCRDetection("Scanned retention: 90 days", 0.98, [{"x": 1}], 2)])
    transcript = TranscriptResult("transcript-1", "", "hi", 0.91, [
        TranscriptSegment("Data retention is 90 days.", 1000, 3500, "A")
    ])
    audio = transcript_to_evidence("doc-2", transcript)
    assert ocr[0].page == 2 and ocr[0].confidence == 0.98
    assert audio[0].start_seconds == 1 and audio[0].speaker == "A" and audio[0].language == "hi"
