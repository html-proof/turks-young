import re


_TIMESTAMP = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")


def _milliseconds(minutes: str, seconds: str, fraction: str | None) -> int:
    fraction_ms = 0
    if fraction:
        fraction_ms = int(fraction.ljust(3, "0")[:3])
    return (int(minutes) * 60 + int(seconds)) * 1000 + fraction_ms


def parse_lrc(lrc: str | None, duration_ms: int | None = None) -> list[dict]:
    """Convert LRC text to ordered, de-duplicated lyric line objects.

    Multiple timestamps on one line are supported. Metadata tags and malformed
    timestamps are ignored. The final line ends at the track duration when known.
    """
    if not lrc:
        return []

    parsed: list[dict] = []
    for raw_line in lrc.splitlines():
        matches = list(_TIMESTAMP.finditer(raw_line))
        if not matches:
            continue
        text = raw_line[matches[-1].end():].strip()
        for match in matches:
            seconds = int(match.group(2))
            if seconds >= 60:
                continue
            parsed.append({
                "startMs": _milliseconds(*match.groups()),
                "text": text,
            })

    # Providers occasionally return duplicate timestamps. Keep the last value.
    by_start = {line["startMs"]: line for line in parsed}
    lines = sorted(by_start.values(), key=lambda line: line["startMs"])
    for index, line in enumerate(lines):
        if index + 1 < len(lines):
            line["endMs"] = lines[index + 1]["startMs"]
        else:
            line["endMs"] = duration_ms if duration_ms and duration_ms >= line["startMs"] else None
    return lines

