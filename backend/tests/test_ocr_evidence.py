from app.jobs import IMAGE_MIME_TYPES
from app.multimodal.image_evidence import ocr_to_evidence
from app.multimodal.ocr import OCRDetection
from app.multimodal.page_render import render_pdf_pages


def test_evidence_ids_are_unique_across_pages():
    """Regression: every page restarted at ocr0, so later pages overwrote earlier ones."""
    page1 = ocr_to_evidence("doc", [OCRDetection("one", 0.9, [], 1), OCRDetection("two", 0.9, [], 1)])
    page2 = ocr_to_evidence("doc", [OCRDetection("three", 0.9, [], 2)])
    ids = [e.id for e in page1 + page2]
    assert len(ids) == len(set(ids))
    assert page2[0].page == 2


def test_standalone_image_evidence_keeps_a_stable_id_without_a_page():
    assert ocr_to_evidence("doc", [OCRDetection("hello", 0.9, [], None)])[0].id == "doc:ocr0"


def test_image_mime_types_are_valid():
    assert IMAGE_MIME_TYPES[".jpg"] == IMAGE_MIME_TYPES[".jpeg"] == "image/jpeg"
    assert IMAGE_MIME_TYPES[".tif"] == IMAGE_MIME_TYPES[".tiff"] == "image/tiff"


def test_only_requested_pages_are_rendered(tmp_path):
    import fitz
    pdf = fitz.open()
    for _ in range(3):
        pdf.new_page()
    path = tmp_path / "three.pdf"
    pdf.save(path); pdf.close()
    rendered = render_pdf_pages(path, tmp_path / "out", pages={2})
    assert [page for page, _ in rendered] == [2]
