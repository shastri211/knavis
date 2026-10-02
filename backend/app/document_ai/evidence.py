from dataclasses import dataclass, field

@dataclass
class VisualEvidence:
    id: str
    source_id: str
    modality: str
    text: str
    page: int | None
    bbox: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

def element_to_evidence(source_id: str, element, text=""):
    return VisualEvidence(
        id=f"{source_id}:page{element.page}:{element.label}",
        source_id=source_id,
        modality=element.label,
        text=text,
        page=element.page,
        bbox=element.bbox,
        metadata={"confidence": element.confidence},
    )

def table_to_evidence(source_id: str, table, markdown_text=""):
    return VisualEvidence(
        id=f"{source_id}:page{table.page}:table",
        source_id=source_id,
        modality="table",
        text=markdown_text,
        page=table.page,
        bbox=table.bbox,
        metadata={
            "cells": table.cells,
            "rows": table.rows,
            "columns": table.columns,
        },
    )
