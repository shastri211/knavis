"""Load a spreadsheet or CSV document into the session's table store."""
import logging
from pathlib import Path

from sqlalchemy.orm import Session

from ..ingest.extractors.legacy import convert_legacy
from ..ingest.extractors.tabular import SheetTable, read_tables
from ..ingest.registry import format_of
from ..models import DataTable, Document
from .tablestore import replace_document_tables

logger = logging.getLogger("mragrag")

TABULAR_KINDS = frozenset({"xlsx", "csv"})


def read_document_tables(path: Path, work_dir: Path) -> list[SheetTable]:
    """The document's sheets as tables. Legacy workbooks (.xls, .ods) are read from their converted copy."""
    path = Path(path)
    fmt = format_of(path)
    source = path
    if fmt and fmt.tier == "legacy":
        source = work_dir / f"{path.stem}{fmt.convert_to}"
        if not source.exists():
            source = convert_legacy(path, fmt.convert_to, work_dir)
    return read_tables(source)


def load_document_tables(db: Session, document: Document, path: Path, work_dir: Path,
                         sheets: list[SheetTable] | None = None) -> list[DataTable]:
    """Replace the document's tables. ``sheets`` are the ones the extractor already read (otherwise the file is read
    again, e.g. when its extraction came from the cache). Analytics is an addition to search, so a failure here never
    fails ingestion."""
    try:
        sheets = read_document_tables(path, work_dir) if sheets is None else sheets
        return replace_document_tables(db, document, sheets)
    except Exception as exc:
        db.rollback()
        logger.warning("Spreadsheet %s could not be loaded for analysis (%s: %s); text search still works",
                       document.id, type(exc).__name__, str(exc)[:120])
        return []
