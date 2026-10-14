import re


def chunk_text(text: str, max_words: int = 180, overlap: int = 30) -> list[str]:
    """Paragraph-aware splitter. Packs paragraphs into ~max_words chunks; a paragraph longer
    than max_words is cut into overlapping windows so context isn't lost at the boundary."""
    if overlap >= max_words:
        raise ValueError("overlap must be smaller than max_words")
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current: list[str] = []
    count = 0

    def flush() -> None:
        nonlocal current, count
        if current:
            chunks.append("\n\n".join(current))
            current, count = [], 0

    for para in paragraphs:
        words = para.split()
        if len(words) > max_words:
            flush()
            step = max_words - overlap
            for i in range(0, len(words), step):
                chunks.append(" ".join(words[i : i + max_words]))
                if i + max_words >= len(words):
                    break
            continue
        if count + len(words) > max_words:
            flush()
        current.append(para)
        count += len(words)
    flush()
    return chunks
