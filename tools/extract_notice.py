"""Turn a tender's notice PDF into validated, structured eligibility details.

pdfplumber gets the text, the LLM fills a fixed JSON schema, and plain Python validates it.
The model must quote the notice text behind any reservation claim; if that quote is not
actually in the PDF text, the claim is discarded ("unknown"), so it can't invent eligibility.

CLI:  python -m tools.extract_notice <tender_id> [<tender_id> ...]
"""

import io
import re
import sys
import time

import pdfplumber
import requests

from tools import llm
from tools.fetch_tenders import HEADERS

MAX_PAGES = 3
MAX_CHARS = 8000
DOC_PREFERENCE = ("Tender Notice", "Tender Document", "Additional Documents")
RESERVED = ("youth", "women", "pwd", "agpo_any", "open", "unknown")

SYSTEM = """You extract facts from Kenyan public tender notices. Reply with ONE JSON object, nothing else.
Only use what the notice text says. If something is not stated, use null (or "unknown" / [] as noted). Never guess.
The notice may list several tenders; report on the one matching the given reference/title.

Schema:
{
 "summary": "<=25 words: what is being bought/built and where",
 "reserved_for": "youth" | "women" | "pwd" | "agpo_any" | "open" | "unknown",
     // youth/women/pwd = reserved for that AGPO group; agpo_any = reserved for AGPO groups generally
     // ("Reserved", "AGPO", "youth, women and PWD"); open = explicitly open to all ("All", "Open");
     // unknown = not stated. In tables, a column headed Eligibility/Category/Reservation/Group whose
     // value for this tender is "OPEN", "ALL" or "NONE" means open; "Reserved"/"AGPO" values mean reserved.
 "reserved_evidence": "exact short quote from the notice supporting reserved_for, or null. If table columns split the words, quote the pieces joined by ...",
 "nca_category": <integer or null>,   // works only: the NCA category limit, e.g. "NCA 6 and above" -> 6
 "required_documents": ["short names, e.g. 'KRA tax compliance certificate', 'NCA registration', 'AGPO certificate'"],
 "bid_security_kes": <number or null>,  // in KES; null if none or not stated
 "site_visit_or_prebid": "date/time and place if stated, else null",
 "location": "town/county where the work or delivery happens, else null"
}"""


def candidate_documents(tender, limit=2):
    """Documents to try, best first (notice, then tender document). Capped to limit downloads."""
    docs = tender.get("documents") or []
    rank = {k: i for i, k in enumerate(DOC_PREFERENCE)}
    ordered = sorted((d for d in docs if d["type"] in rank), key=lambda d: rank[d["type"]])
    return (ordered or docs)[:limit]


def pdf_text(content, max_pages=MAX_PAGES):
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        return "\n".join((p.extract_text() or "") for p in pdf.pages[:max_pages])


MAX_BYTES = 12_000_000   # full tender documents can be huge; we only need the first pages
MAX_SECONDS = 90         # the portal can be very slow on big files; skip rather than stall the run


def download(url, max_seconds=MAX_SECONDS):
    started = time.monotonic()
    with requests.get(url, headers=HEADERS, timeout=(10, 30), stream=True) as r:
        r.raise_for_status()
        if int(r.headers.get("Content-Length") or 0) > MAX_BYTES:
            raise ValueError("document too large")
        buf = bytearray()
        for chunk in r.iter_content(64 * 1024):
            buf += chunk
            if len(buf) > MAX_BYTES:
                raise ValueError("document too large")
            if time.monotonic() - started > max_seconds:
                raise TimeoutError("download too slow")
        return bytes(buf)


def focus(text, tender, limit=MAX_CHARS):
    """Keep the text within budget, centred on this tender's reference when the notice lists many."""
    if len(text) <= limit:
        return text
    ref = (tender.get("tender_ref") or "")[:12]
    i = text.find(ref) if ref else -1
    if i == -1:
        return text[:limit]
    start = max(0, i - limit // 3)
    return text[start : start + limit]


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    digits = re.sub(r"[^\d.]", "", str(v))
    try:
        return float(digits) if digits and float(digits) > 0 else None
    except ValueError:
        return None


EVIDENCE_WINDOW = 300  # max characters spanned by the fragments of one quote


def grounded(evidence, text):
    """True if the quote is really in the text. PDF tables interleave columns, so a quote may skip
    words with '...'; then every fragment must appear, close together, in the notice."""
    norm_text = _norm(text)
    frags = [_norm(f) for f in re.split(r"\.{2,}|…", evidence or "")]
    frags = [f for f in frags if f]
    if not frags:
        return False
    first = norm_text.find(frags[0])
    while first != -1:
        pos, ok = first + len(frags[0]), True
        for f in frags[1:]:
            nxt = norm_text.find(f, pos)
            if nxt == -1 or nxt + len(f) - first > EVIDENCE_WINDOW:
                ok = False
                break
            pos = nxt + len(f)
        if ok:
            return True
        first = norm_text.find(frags[0], first + 1)
    return False


def validate(raw, text):
    """Coerce model output into the schema and drop claims the text doesn't support."""
    reserved = str(raw.get("reserved_for") or "unknown").lower().strip()
    if reserved not in RESERVED:
        reserved = "unknown"
    evidence = raw.get("reserved_evidence")
    evidence = str(evidence).strip() if evidence else None
    if reserved in ("youth", "women", "pwd", "agpo_any") and not grounded(evidence, text):
        reserved, evidence = "unknown", None  # the model's claim isn't backed by the notice text

    nca = raw.get("nca_category")
    try:
        nca = int(nca) if nca is not None and str(nca).strip() != "" else None
    except (TypeError, ValueError):
        m = re.search(r"\d+", str(nca))
        nca = int(m.group()) if m else None
    if nca is not None and not 1 <= nca <= 8:
        nca = None

    docs = raw.get("required_documents") or []
    if not isinstance(docs, list):
        docs = [docs]
    docs = [str(d).strip() for d in docs if str(d).strip()][:12]

    def s(key, n=200):
        v = raw.get(key)
        return str(v).strip()[:n] if v not in (None, "", "null") else None

    return {
        "summary": s("summary", 300),
        "reserved_for": reserved,
        "reserved_evidence": evidence,
        "nca_category": nca,
        "required_documents": docs,
        "bid_security_kes": _num(raw.get("bid_security_kes")),
        "site_visit_or_prebid": s("site_visit_or_prebid"),
        "location": s("location"),
    }


def extract_from_text(text, tender):
    user = (
        f"Tender reference: {tender.get('tender_ref')}\nTitle: {tender.get('title')}\n"
        f"Procuring entity: {tender.get('entity')}\n\nNOTICE TEXT:\n{focus(text, tender)}"
    )
    return validate(llm.chat_json(SYSTEM, user), text)


def cache_key(tender):
    docs = candidate_documents(tender)
    return docs[0]["url"] if docs else None  # the tender's primary document


def cacheable(result):
    return result.get("status") != "failed"  # transient failures (network, quota) get retried next run


def extract(tender, db=None):
    """Structured details for one tender. Cached by document URL when a db is given."""
    from tools import store

    key = cache_key(tender)
    if db is not None and key and (cached := store.get_extraction(db, key)):
        return cached
    result = extract_uncached(tender)
    if db is not None and key and cacheable(result):
        store.save_extraction(db, key, result)
    return result


def extract_uncached(tender):
    """Download + read + LLM. Thread-safe: touches no database."""
    docs = candidate_documents(tender)
    if not docs:
        return {"status": "no_document"}
    result = None
    for doc in docs:
        try:
            text = pdf_text(download(doc["url"]))
        except Exception as e:  # network or broken PDF: try the next document, don't crash the run
            result = {"status": "failed", "error": str(e)[:200], "doc_url": doc["url"]}
            continue
        if len(text.strip()) < 200:  # scanned image PDF, no text layer
            result = {"status": "no_text", "doc_url": doc["url"], "doc_type": doc["type"]}
            continue
        try:
            result = dict(extract_from_text(text, tender), status="ok", doc_url=doc["url"], doc_type=doc["type"])
        except Exception as e:  # LLM quota/rate limit etc.: report as transient
            result = {"status": "failed", "error": f"llm: {str(e)[:180]}", "doc_url": doc["url"]}
        break
    return result


def main(ids):
    import json

    from tools import store

    db = store.connect()
    by_id = {t["id"]: t for t in store.open_tenders(db)}
    for i in ids:
        t = by_id.get(int(i))
        if not t:
            print(f"{i}: not found among open tenders")
            continue
        print(f"\n== {t['tender_ref']} | {t['title'][:80]}")
        print(json.dumps(extract(t, db), indent=1))


if __name__ == "__main__":
    main(sys.argv[1:])
