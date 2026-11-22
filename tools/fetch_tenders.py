"""Fetch active tenders from Kenya's Public Procurement Information Portal (tenders.go.ke).

Uses the same public JSON endpoint the portal's own web page calls. Every record is
reduced to an ALLOW-LIST of tender fields: the raw API response also carries personal
data about procurement officers (ID numbers, phone numbers, emails), which this tool
never keeps, stores or prints.

CLI:  python -m tools.fetch_tenders            # fetch, save to .tmp/tenders.db, print a summary
"""

import time

import requests

BASE = "https://tenders.go.ke"
ACTIVE_URL = f"{BASE}/api/active-tenders"
COUNTIES_URL = f"{BASE}/api/counties"
TENDER_PAGE = f"{BASE}/tenders"  # public listing page; tenders are searchable there by reference
HEADERS = {
    "User-Agent": "tender-finder/1.0 (open-source tender alert tool; polite daily fetch)",
    "Accept": "application/json",
}
PAGE_SIZE = 100
DELAY_S = 1.0  # be polite: one request per second

# The only fields that leave this module. Anything not listed here is dropped.
FIELDS = (
    "id", "ocid", "tender_ref", "title", "published_at", "close_at", "tender_fee",
    "bid_security_value", "category", "method", "entity", "entity_type", "county",
    "addendum", "submission", "documents",
)


def _wait_seconds(resp, attempt):
    """Honour the portal's Retry-After (it rate-limits at 60 API requests/minute), else back off."""
    try:
        retry_after = float(resp.headers.get("Retry-After")) if resp is not None else 0
    except (TypeError, ValueError):
        retry_after = 0
    return max(retry_after + 1, 5 * 2 ** attempt)  # 5, 10, 20, 40 s


def _get(url, params=None, retries=5, session=None, sleep=time.sleep):
    s = session or requests
    for attempt in range(retries):
        resp = None
        try:
            resp = s.get(url, params=params, headers=HEADERS, timeout=60)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError):
            if attempt == retries - 1:
                raise
            sleep(_wait_seconds(resp, attempt))


def _title(obj):
    return (obj or {}).get("title") if isinstance(obj, dict) else None


def clean(raw, counties=None):
    """Reduce one raw API record to the allow-listed schema."""
    pe = raw.get("pe") or {}
    # The entity *type* is only nested under the creator record; read that one field, nothing else.
    pe_type = pe.get("type") or (((raw.get("created_by") or {}).get("pe") or {}).get("type")) or {}
    docs = []
    for d in raw.get("documents") or []:
        url = d.get("url")
        if url:
            docs.append({
                "type": ((d.get("type") or {}).get("description") or "Document").strip(),
                "url": url if url.startswith("http") else BASE + url,
            })
    out = {
        "id": raw.get("id"),
        "ocid": raw.get("ocid"),
        "tender_ref": (raw.get("tender_ref") or "").strip(),
        "title": " ".join((raw.get("title") or "").split()),
        "published_at": raw.get("published_at"),
        "close_at": raw.get("close_at"),
        "tender_fee": raw.get("tender_fee") or 0,
        "bid_security_value": raw.get("bid_security_value"),
        "category": _title(raw.get("procurement_category")),
        "method": _title(raw.get("procurement_method")),
        "entity": (pe.get("name") or "").strip(),
        "entity_type": pe_type.get("description"),
        "county": (counties or {}).get(pe.get("county_id")),
        "addendum": bool(raw.get("addendum_added")),
        "submission": ", ".join(sorted({_title(m) for m in raw.get("submission_methods") or [] if _title(m)})),
        "documents": docs,
    }
    assert tuple(out) == FIELDS
    return out


PAST_BIDDING_MARKERS = (
    "opening minutes", "evaluation", "award", "professional opinion", "appointment letter",
    "due diligence", "inspection", "termination", "negotiation",
)


def past_bidding(tender):
    """The portal keeps some tenders 'active' after bids were opened or the tender was awarded/terminated.
    Post-opening documents (minutes, evaluation, award, termination...) give that away."""
    return any(m in d["type"].lower() for d in tender.get("documents") or [] for m in PAST_BIDDING_MARKERS)


def fetch_counties(session=None):
    """{portal county id: county name}"""
    return {c["id"]: c["name"] for c in _get(COUNTIES_URL, session=session)}


def fetch_active(max_pages=None, session=None, progress=None):
    """Return all active tenders as cleaned dicts."""
    session = session or requests.Session()
    counties = fetch_counties(session)
    tenders, page, last = [], 1, 1
    while page <= last:
        time.sleep(DELAY_S if page > 1 else 0)
        data = _get(ACTIVE_URL, params={"perpage": PAGE_SIZE, "page": page}, session=session)
        last = data.get("last_page") or 1
        tenders += [clean(r, counties) for r in data.get("data", [])]
        if progress:
            progress(page, last)
        if max_pages and page >= max_pages:
            break
        page += 1
    return tenders


def main():
    from tools import store

    tenders = fetch_active(progress=lambda p, n: print(f"  page {p}/{n}", end="\r"))
    db = store.connect()
    new = store.upsert_tenders(db, tenders)
    print(f"\nFetched {len(tenders)} active tenders, {len(new)} new. Saved to {store.DB_PATH}")


if __name__ == "__main__":
    main()
