"""PDF originals and text; keep parsing isolated from the collector process."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.parse import parse_qs, unquote, urlsplit


def is_document_url(url):
    p = urlsplit(url)
    return (p.path.lower().endswith((".pdf", "/download.html"))
            or parse_qs(p.query).get("task") == ["document.viewdoc"])


def extract_pdf(url, body, site):
    from crawler import config
    if not body.startswith(b"%PDF-"):
        raise ValueError("invalid PDF signature")
    digest = hashlib.sha256(body).hexdigest()
    directory = Path(config.DATA) / site["domain"] / "attachments"
    directory.mkdir(parents=True, exist_ok=True)
    original = directory / (digest + ".pdf")
    if not original.exists():
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as f:
            f.write(body)
        os.replace(f.name, original)
    # The existing HTTP cap bounds input size; a child process bounds parse time.
    result = subprocess.run([sys.executable, __file__, str(original)], capture_output=True,
                            text=True, timeout=float(os.environ.get("PDF_PARSE_TIMEOUT", "60")))
    if result.returncode:
        raise ValueError("PDF parse failed: " + result.stderr[-300:])
    data = json.loads(result.stdout)
    row = {"url": url, "host": site["domain"], "title": data["title"] or unquote(urlsplit(url).path.rsplit("/", 1)[-1]),
           "text": data["text"], "description": data["subject"], "user_author_name": data["author"],
           "link": url, "country_text": site.get("country", ""), "lang_text": site.get("lang", "")}
    receipt = {"sha256": digest, "source_url": url, "pages": data["pages"],
               "text_status": "extracted" if data["text"].strip() else "no_text_layer"}
    # One receipt per source URL, one original per content hash.
    key = hashlib.sha256(url.encode()).hexdigest()
    with tempfile.NamedTemporaryFile(dir=directory, mode="w", delete=False) as f:
        json.dump(receipt, f, ensure_ascii=False)
    os.replace(f.name, directory / (key + ".json"))
    return row


if __name__ == "__main__":
    from pypdf import PdfReader
    reader = PdfReader(sys.argv[1])
    metadata = reader.metadata
    print(json.dumps({"title": (metadata.title or "") if metadata else "",
                      "subject": (metadata.subject or "") if metadata else "",
                      "author": (metadata.author or "") if metadata else "",
                      "pages": len(reader.pages),
                      "text": "\n\n".join(page.extract_text() or "" for page in reader.pages)}, ensure_ascii=False))
