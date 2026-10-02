from pathlib import Path

import fitz

from app.ingest.extract import extract_document
from app.ingest.registry import detect_type
from app.multimodal.assemblyai import TranscriptResult, TranscriptSegment
from app.multimodal.audio_evidence import transcript_to_evidence
from app.multimodal.image_evidence import ocr_to_evidence
from app.multimodal.ocr import OCRDetection


def test_detect_types():
    assert detect_type(Path('a.pdf')) == 'pdf'
    assert detect_type(Path('a.jpg')) == 'image'
    assert detect_type(Path('a.wav')) == 'audio'
    assert detect_type(Path('a.exe')) == 'unknown'


def test_pdf_extraction_preserves_page_metadata(tmp_path):
    path = tmp_path / "retention.pdf"
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((72, 72), "Company data must be retained for 90 days.")
    pdf.save(path)
    pdf.close()
    result = extract_document(path)
    assert result.kind == "pdf" and result.info["pages"] == 1
    assert result.elements[0].page == 1 and "90 days" in result.elements[0].text


def test_ocr_and_audio_evidence_preserve_metadata():
    ocr = ocr_to_evidence("doc-1", [OCRDetection("Scanned retention: 90 days", 0.98, [{"x": 1}], 2)])
    transcript = TranscriptResult("transcript-1", "", "hi", 0.91, [
        TranscriptSegment("Data retention is 90 days.", 1000, 3500, "A")
    ])
    audio = transcript_to_evidence("doc-2", transcript)
    assert ocr[0].page == 2 and ocr[0].confidence == 0.98
    assert audio[0].start_seconds == 1 and audio[0].speaker == "A" and audio[0].language == "hi"
