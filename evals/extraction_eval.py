"""Measure extraction accuracy against hand-labelled public tender notices.

Downloads each labelled notice, runs the real extraction (fresh LLM call, no cache),
and compares the fields that drive eligibility decisions.

Run:  python -m evals.extraction_eval            (about 20 LLM calls on the free tier)
"""

import hashlib
import json
from pathlib import Path

from tools.extract_notice import download, extract_from_text, pdf_text

LABELS = Path(__file__).with_name("labels.json")
CACHE = Path(__file__).resolve().parent.parent / ".tmp" / "eval_text"  # local only, never committed
FIELDS = ("reserved_for", "nca_category", "bid_security_kes")


def same(field, want, got):
    if field == "bid_security_kes" and want is not None and got is not None:
        return abs(float(want) - float(got)) < 1
    return want == got


def main():
    items = json.loads(LABELS.read_text(encoding="utf-8"))["items"]
    totals = {f: [0, 0] for f in FIELDS}
    misses = []
    for it in items:
        cache = CACHE / (hashlib.sha1(it["doc_url"].encode()).hexdigest()[:16] + ".txt")
        if cache.exists():
            text = cache.read_text(encoding="utf-8")
        else:
            text = pdf_text(download(it["doc_url"], max_seconds=600))  # the portal can be very slow
            CACHE.mkdir(parents=True, exist_ok=True)
            cache.write_text(text, encoding="utf-8")
        got =extract_from_text(text, {"tender_ref": it["tender_ref"], "title": it.get("title", ""), "entity": ""})
        row = []
        for f in FIELDS:
            if it[f] == "skip":
                row.append(f"{f}=skip")
                continue
            ok = same(f, it[f], got[f])
            totals[f][0] += ok
            totals[f][1] += 1
            row.append(f"{f}={'✓' if ok else '✗'}")
            if not ok:
                misses.append(f"{it['tender_ref']}: {f} expected {it[f]!r}, got {got[f]!r}")
        print(f"{it['tender_ref']:<34} " + "  ".join(row))

    print("\n| Field | Correct | Accuracy |\n|---|---|---|")
    for f, (ok, n) in totals.items():
        print(f"| `{f}` | {ok}/{n} | {ok / n:.0%} |")
    if misses:
        print("\nMisses:\n" + "\n".join(f"- {m}" for m in misses))


if __name__ == "__main__":
    main()
