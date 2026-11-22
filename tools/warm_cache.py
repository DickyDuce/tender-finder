"""Pre-read tender notices so the web app answers instantly.

Runs the matching pipeline for every profile in profiles/ (which extracts and caches the
notices of their top candidates), then exports the cached extractions for currently open
tenders to one JSON file. A scheduled GitHub Action publishes that file on the `cache`
branch, and the app downloads it at start-up (see CACHE_URL).

The file holds only fields read from public tender notices: no API personal data.

CLI:  python -m tools.warm_cache [--out .tmp/cache_export/extractions.json]
"""

import argparse
import json
from datetime import datetime
from pathlib import Path

from tools import fetch_tenders, store
from tools.extract_notice import cache_key
from tools.match import load_profile, match

PROFILES_DIR = Path(__file__).resolve().parent.parent / "profiles"
CACHE_URL = "https://raw.githubusercontent.com/DickyDuce/tender-finder/cache/extractions.json"
DEFAULT_OUT = Path(__file__).resolve().parent.parent / ".tmp" / "cache_export" / "extractions.json"


def warm(db, tenders, profiles, progress=None):
    """Extract (and cache) notices for each profile's candidates. Returns the number of profiles run."""
    for p in profiles:
        match(tenders, p, db=db, use_llm=True, top=25, progress=progress, rank=False)
    return len(profiles)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    a = ap.parse_args()

    db = store.connect()
    store.upsert_tenders(db, fetch_tenders.fetch_active())
    open_t = store.open_tenders(db)
    profiles = [load_profile(p) for p in sorted(PROFILES_DIR.glob("*.yaml"))]
    warm(db, open_t, profiles, progress=lambda i, n: print(f"  notice {i}/{n}", end="\r"))

    data = store.export_extractions(db, {cache_key(t) for t in open_t})
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"generated": datetime.now().isoformat(timespec="seconds"), "extractions": data}),
                   encoding="utf-8")
    print(f"\nExported {len(data)} extractions for {len(open_t)} open tenders to {out}")


if __name__ == "__main__":
    main()
