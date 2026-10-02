from dataclasses import dataclass
import re

@dataclass
class Chunk:
    id: str
    source_id: str
    text: str
    metadata: dict

def chunk_text(source_id: str, text: str, metadata=None, max_chars=1800, overlap=250):
    metadata = metadata or {}
    clean = re.sub(r"\s+", " ", text).strip()
    if not clean:
        return []
    chunks = []
    start = 0
    n = len(clean)
    i = 0
    while start < n:
        end = min(start + max_chars, n)
        if end < n:
            cut = clean.rfind(" ", start, end)
            if cut > start + max_chars // 2:
                end = cut
        part = clean[start:end].strip()
        if part:
            chunks.append(Chunk(
                id=f"{source_id}:c{i}",
                source_id=source_id,
                text=part,
                metadata=dict(metadata),
            ))
            i += 1
        if end >= n:
            break
        start = max(0, end - overlap)
    return chunks
