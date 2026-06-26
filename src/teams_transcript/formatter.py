from __future__ import annotations

from teams_transcript.extractor import TranscriptEntry


def format_as_text(entries: list[TranscriptEntry], *, group_by_speaker: bool = False) -> str:
    if not entries:
        return ""

    if not group_by_speaker:
        return "\n".join(
            f"[{e.timestamp}] {e.speaker}: {e.text}" if e.timestamp else f"{e.speaker}: {e.text}"
            for e in entries
        )

    lines: list[str] = []
    prev_speaker = None
    block: list[str] = []
    block_ts = ""

    def flush():
        if block:
            header = f"[{block_ts}] {prev_speaker}:" if block_ts else f"{prev_speaker}:"
            lines.append(header)
            for t in block:
                lines.append(f"  {t}")
            lines.append("")

    for entry in entries:
        if entry.speaker != prev_speaker:
            flush()
            prev_speaker = entry.speaker
            block = [entry.text]
            block_ts = entry.timestamp
        else:
            block.append(entry.text)

    flush()
    return "\n".join(lines).rstrip()
