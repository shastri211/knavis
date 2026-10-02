from app.ingest.chunker import HARD_MAX, MIN_TAIL, TARGET, build_chunks, split_text
from app.ingest.elements import HEADING, PARAGRAPH, SLIDE, SUMMARY, TABLE, TRANSCRIPT, Element


def para(i, text, **kw):
    return Element(id=f"e{i}", kind=PARAGRAPH, text=text, **kw)


def heading(i, text, level):
    return Element(id=f"e{i}", kind=HEADING, text=text, level=level)


def test_small_paragraphs_merge_up_to_the_target_size():
    chunks = build_chunks([para(i, "word " * 40) for i in range(10)])   # 10 x ~200 chars
    assert len(chunks) == 2 and all(len(c.text) <= TARGET for c in chunks)
    assert chunks[0].element_ids == ["e0", "e1", "e2", "e3", "e4"]


def test_chunks_never_merge_across_pages_slides_or_logical_documents():
    by_page = build_chunks([para(0, "first page text here", page=1), para(1, "second page text here", page=2)])
    assert [c.page for c in by_page] == [1, 2]
    by_group = build_chunks([para(0, "part one text", group="logical:1"), para(1, "part two text", group="logical:2")])
    assert len(by_group) == 2


def test_headings_become_a_section_breadcrumb_not_chunks():
    chunks = build_chunks([
        heading(0, "Policies", 1), heading(1, "Retention", 2), para(2, "Keep data for 90 days."),
        heading(3, "Backups", 2), para(4, "Keep backups for 30 days."),
        heading(5, "Security", 1), para(6, "Use strong passwords."),
    ])
    assert [c.section for c in chunks] == ["Policies > Retention", "Policies > Backups", "Security"]
    assert chunks[0].text.startswith("Section: Policies > Retention\n") and "Keep data for 90 days." in chunks[0].text


def test_a_tiny_trailing_piece_is_merged_into_its_neighbour():
    chunks = build_chunks([para(0, "a" * 1190), para(1, "short tail")])
    assert len(chunks) == 1 and "short tail" in chunks[0].text
    assert MIN_TAIL > len("short tail")


def test_long_paragraph_is_split_on_sentences_with_overlap():
    text = " ".join(f"Sentence number {i} says something useful." for i in range(120))
    pieces = split_text(text)
    assert len(pieces) > 3 and all(len(p) <= TARGET for p in pieces)
    assert all(p.endswith(".") for p in pieces)                      # never cut mid-sentence
    assert pieces[0].split(". ")[-1] in pieces[1]                    # last sentence is carried over
    chunks = build_chunks([para(0, text)])
    assert all(len(c.text) <= HARD_MAX for c in chunks) and len(chunks) == len(pieces)


def test_text_without_punctuation_is_still_cut_at_word_boundaries():
    pieces = split_text("word " * 1500)
    assert all(len(p) <= TARGET for p in pieces) and all(not p.endswith("wor") for p in pieces)


def test_big_tables_split_by_rows_and_repeat_the_header():
    rows = [["Name", "Qty"]] + [[f"item-{i}", str(i)] for i in range(1, 301)]
    el = Element(id="t", kind=TABLE, text="", rows=rows, page=3, locator="page 3")
    chunks = build_chunks([el])
    assert len(chunks) > 3 and all(c.text.startswith("Name | Qty\n") and c.page == 3 for c in chunks)
    assert chunks[0].locator == "page 3, rows 2-" + chunks[0].locator.rsplit("-", 1)[1]
    last_of_first = int(chunks[0].locator.rsplit("-", 1)[1])
    assert chunks[1].locator.startswith(f"page 3, rows {last_of_first + 1}-")   # contiguous, nothing lost
    assert "item-300" in chunks[-1].text and chunks[-1].locator.endswith("301")


def test_pre_grouped_spreadsheet_rows_keep_their_sheet_and_row_range():
    el = Element(id="r5", kind=TABLE, text="", rows=[["a", "b"], ["1", "2"], ["3", "4"]], sheet="Policy",
                 locator="sheet Policy, rows 5-6", meta={"row_start": 5, "row_end": 6})
    (chunk,) = build_chunks([el])
    assert chunk.sheet == "Policy" and chunk.locator == "sheet Policy, rows 5-6" and chunk.text == "a | b\n1 | 2\n3 | 4"


def test_slides_and_summaries_stay_whole_without_a_section_line():
    chunks = build_chunks([
        heading(0, "Ignored heading", 1),
        Element(id="s2", kind=SLIDE, text="Title\nBody", slide=2, locator="slide 2"),
        Element(id="sum", kind=SUMMARY, text="Sheet 'X': 3 rows", sheet="X"),
    ])
    assert [c.kind for c in chunks] == [SLIDE, SUMMARY]
    assert chunks[0].slide == 2 and not chunks[0].text.startswith("Section:")


def test_transcript_cues_merge_into_time_windows():
    cues = [Element(id=f"c{i}", kind=TRANSCRIPT, text=f"cue number {i}", locator="x", meta={"start_s": i * 20, "end_s": i * 20 + 5}) for i in range(10)]
    chunks = build_chunks(cues)
    assert 2 <= len(chunks) < 10
    assert chunks[0].locator.startswith("00:00-") and chunks[0].meta["start_s"] == 0
    assert all(c.meta["end_s"] - c.meta["start_s"] <= 125 for c in chunks)


def test_chunking_is_deterministic():
    els = [heading(0, "A", 1)] + [para(i, f"paragraph {i} " * 30) for i in range(1, 12)]
    assert build_chunks(els) == build_chunks(els)
    assert [c.ordinal for c in build_chunks(els)] == list(range(len(build_chunks(els))))


def test_no_chunk_exceeds_the_hard_maximum_even_with_the_section_line():
    long_title = "A very long section title " * 8
    els = [heading(0, long_title, 1)] + [para(i, "filler words go here. " * 55) for i in range(1, 8)]
    els.append(para(99, "z" * 1700))
    assert all(len(c.text) <= HARD_MAX for c in build_chunks(els))
