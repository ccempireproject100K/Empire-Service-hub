"""Document ingestion for tenant knowledge bases (v2, section B).

Operators feed the hub real documents — help-center exports, policy PDFs
exported as text, FAQ pages — instead of hand-writing every article.
Supported sources: pasted/uploaded text in txt/md/html/csv/json form, or a
single web page fetched by URL. Documents are split into overlapping
chunks; each chunk becomes retrievable knowledge (see
``knowledge.search_hybrid``) and is embedded at ingest time when an
embeddings provider is configured.

Ingestion is deliberately format-tolerant and dependency-free: HTML is
reduced to text with the stdlib parser, CSV rows become "col: value"
lines, JSON is pretty-printed. Binary formats (PDF/DOCX) are out of scope
— export them to text/markdown first.
"""
import csv
import io
import json
import re
from html.parser import HTMLParser

import httpx

import db as db_module
from lib import embeddings
from lib.util import new_id, now

CHUNK_CHARS = 900
CHUNK_OVERLAP = 120


class _TextExtractor(HTMLParser):
    """Minimal HTML → text: skips script/style, keeps block spacing."""

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("p", "div", "br", "li", "h1", "h2", "h3", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def to_text(content: str, source_type: str) -> str:
    """Normalize a raw document body to plain text by declared type."""
    source_type = (source_type or "txt").lower()
    if source_type in ("html", "htm"):
        parser = _TextExtractor()
        parser.feed(content or "")
        text = "".join(parser.parts)
    elif source_type == "csv":
        rows = []
        for row in csv.reader(io.StringIO(content or "")):
            rows.append(" | ".join(cell.strip() for cell in row if cell.strip()))
        text = "\n".join(rows)
    elif source_type == "json":
        try:
            text = json.dumps(json.loads(content), indent=1, ensure_ascii=False)
        except (ValueError, TypeError):
            text = content or ""
    else:  # txt / md / anything else: as-is
        text = content or ""
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def chunk_text(text: str, size: int = CHUNK_CHARS,
               overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping chunks, preferring paragraph breaks."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            boundary = text.rfind("\n", start + size // 2, end)
            if boundary > start:
                end = boundary
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


async def fetch_url(url: str) -> str:
    """Fetch one page and reduce it to text (single page, no crawling)."""
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": "ServiceHub/2.0"})
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
    if "html" in content_type:
        return to_text(resp.text, "html")
    return to_text(resp.text, "txt")


async def ingest(database, tenant_id: str, title: str, text: str,
                 source_type: str = "upload", source_ref: str | None = None) -> dict:
    """Store a document + its chunks; embed chunks when a provider exists."""
    chunks = chunk_text(text)
    doc = {
        "_id": new_id("doc_"),
        "tenant_id": tenant_id,
        "title": title,
        "source_type": source_type,
        "source_ref": source_ref,
        "chunk_count": len(chunks),
        "char_count": len(text),
        "created_at": now(),
    }
    await database[db_module.DOCUMENTS].insert_one(doc)

    vectors = None
    if chunks and embeddings.available():
        vectors = await embeddings.embed_texts(
            [f"{title}\n{chunk}"[:4000] for chunk in chunks]
        )
    for index, chunk in enumerate(chunks):
        chunk_doc = {
            "_id": new_id("chk_"),
            "tenant_id": tenant_id,
            "document_id": doc["_id"],
            "title": title,
            "index": index,
            "content": chunk,
            "created_at": now(),
        }
        if vectors:
            chunk_doc["embedding"] = vectors[index]
        await database[db_module.DOC_CHUNKS].insert_one(chunk_doc)
    doc["embedded"] = bool(vectors)
    return doc


async def delete_document(database, tenant_id: str, document_id: str) -> bool:
    result = await database[db_module.DOCUMENTS].delete_one(
        {"_id": document_id, "tenant_id": tenant_id}
    )
    if result.deleted_count == 0:
        return False
    chunks = await database[db_module.DOC_CHUNKS].find(
        {"document_id": document_id, "tenant_id": tenant_id}
    ).to_list(length=10000)
    for chunk in chunks:
        await database[db_module.DOC_CHUNKS].delete_one({"_id": chunk["_id"]})
    return True
