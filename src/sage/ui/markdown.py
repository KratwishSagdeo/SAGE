"""Small, dependency-free renderer for the formatting used in assistant replies."""

from __future__ import annotations

import re


def _inline(text: str, tags: tuple[str, ...] = (), partial: bool = False) -> list[tuple[str, tuple[str, ...]]]:
    spans: list[tuple[str, tuple[str, ...]]] = []
    plain: list[str] = []

    def flush() -> None:
        if plain:
            spans.append(("".join(plain), tags))
            plain.clear()

    index = 0
    while index < len(text):
        if text[index] == "\\" and index + 1 < len(text) and text[index + 1] in "*_`":
            plain.append(text[index + 1])
            index += 2
            continue
        marker = next((value for value in ("**", "__", "`", "*") if text.startswith(value, index)), None)
        if marker:
            end = text.find(marker, index + len(marker))
            if end >= 0:
                flush()
                body = text[index + len(marker):end]
                tag = "bold" if len(marker) == 2 else "code" if marker == "`" else "italic"
                if tag == "code":
                    spans.append((body, tags + (tag,)))
                else:
                    spans.extend(_inline(body, tags + (tag,), partial))
                index = end + len(marker)
                continue
            if partial or marker in {"**", "__", "`"}:
                flush()
                tag = "bold" if len(marker) == 2 else "code" if marker == "`" else "italic"
                spans.append((text[index + len(marker):], tags + (tag,)))
                return spans
        plain.append(text[index])
        index += 1
    flush()
    return spans


def markdown_spans(message: str, *, partial: bool = False) -> list[tuple[str, tuple[str, ...]]]:
    """Return text/tag spans; preserve Windows paths and user-visible literal text."""
    spans: list[tuple[str, tuple[str, ...]]] = []
    fenced = False
    for line in message.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            spans.append((line, ("code",)))
            continue
        heading = re.match(r"^\s{0,3}#{1,6}\s+", line)
        tags = ("heading",) if heading else ()
        if heading:
            line = line[heading.end():]
        line = re.sub(r"^(\s*)[-*+]\s+", r"\1• ", line)
        spans.extend(_inline(line, tags, partial))
    return spans
