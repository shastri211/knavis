from dataclasses import dataclass

@dataclass
class AudioEvidence:
    id: str
    source_id: str
    text: str
    start_seconds: float
    end_seconds: float
    speaker: str | None
    language: str | None

def transcript_to_evidence(source_id: str, transcript) -> list[AudioEvidence]:
    out = []
    for i, seg in enumerate(transcript.segments):
        if not seg.text.strip():
            continue
        out.append(AudioEvidence(
            id=f"{source_id}:t{i}",
            source_id=source_id,
            text=seg.text.strip(),
            start_seconds=seg.start_ms / 1000,
            end_seconds=seg.end_ms / 1000,
            speaker=seg.speaker,
            language=transcript.language_code,
        ))
    return out
