"""Match open tenders to a business profile.

Pipeline (cheap, deterministic steps first so the free LLM quota goes further):
  1. hard rules      deadline, procurement category, county, exclusions          (Python)
  2. prefilter       BM25 keyword relevance on title + entity, keep top K       (Python)
  3. extraction      notice PDF -> reservation, NCA category, bid security...   (LLM, cached)
  4. eligibility     AGPO group, NCA category, bid security vs the profile     (Python)
  5. ranking         fit score 0-10 + one-line reason, one batched call       (LLM)

CLI:  python -m tools.match profiles/women_contractor_kiambu.yaml [--top 10] [--no-llm]
"""

import argparse
import math
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from tools import llm
from tools.fetch_tenders import past_bidding

PREFILTER_K = 25
STOPWORDS = set("a an and the of for to in at on by with from no nos supply delivery provision proposed tender".split())


# ---------- profile ----------

def load_profile(path_or_dict):
    p = dict(path_or_dict) if isinstance(path_or_dict, dict) else yaml.safe_load(Path(path_or_dict).read_text(encoding="utf-8"))
    p.setdefault("id", Path(path_or_dict).stem if not isinstance(path_or_dict, dict) else p.get("name", "custom"))
    p.setdefault("keywords", [])
    p.setdefault("exclude_keywords", [])
    p.setdefault("categories", [])
    p.setdefault("counties", [])
    p.setdefault("county_strict", False)
    p.setdefault("agpo_group", None)
    p.setdefault("nca_category", None)
    p.setdefault("max_bid_security_kes", None)
    p.setdefault("min_days_to_close", 2)
    return p


# ---------- 1. hard rules ----------

def passes_rules(t, p, today):
    if t["close_at"] < (today + timedelta(days=p["min_days_to_close"])).strftime("%Y-%m-%d"):
        return False
    if past_bidding(t):
        return False
    if p["categories"] and t.get("category") not in p["categories"]:
        return False
    if p["county_strict"] and p["counties"] and t.get("county") not in p["counties"]:
        return False
    title = t["title"].lower()
    if any(x.lower() in title for x in p["exclude_keywords"]):
        return False
    return True


# ---------- 2. BM25 prefilter ----------

def tokens(text):
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOPWORDS and len(w) > 1]


def bm25_scores(docs, query, k1=1.5, b=0.75):
    """Plain BM25: no extra dependencies, fine for a few hundred short titles."""
    toks = [tokens(d) for d in docs]
    n = len(toks) or 1
    avgdl = sum(map(len, toks)) / n or 1
    df = Counter(w for ts in toks for w in set(ts))
    q = set(query)
    scores = []
    for ts in toks:
        tf = Counter(ts)
        s = 0.0
        for w in q & tf.keys():
            idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
            s += idf * tf[w] * (k1 + 1) / (tf[w] + k1 * (1 - b + b * len(ts) / avgdl))
        scores.append(s)
    return scores


def prefilter(tenders, p, k=PREFILTER_K):
    query = [w for kw in p["keywords"] for w in tokens(kw)]
    scores = bm25_scores([f"{t['title']} {t.get('entity', '')}" for t in tenders], query)
    ranked = sorted(zip(scores, tenders), key=lambda x: -x[0])
    return [dict(t, keyword_score=round(s, 2)) for s, t in ranked if s > 0][:k]


# ---------- 4. eligibility ----------

def eligibility(ext, p):
    """Return (eligible, notes). Unknowns stay eligible but are flagged for the user to check."""
    notes, ok = [], True
    if not ext or ext.get("status") != "ok":
        return True, ["Notice could not be read automatically. Check eligibility in the tender document."]

    reserved = ext.get("reserved_for", "unknown")
    group = p.get("agpo_group")
    if reserved in ("youth", "women", "pwd"):
        if group == reserved:
            notes.append(f"Reserved for {reserved}: you qualify (AGPO {group}).")
        else:
            ok = False
            notes.append(f"Reserved for {reserved} only.")
    elif reserved == "agpo_any":
        if group:
            notes.append(f"Reserved for AGPO groups: you qualify ({group}).")
        else:
            ok = False
            notes.append("Reserved for AGPO groups (youth, women, PWD).")
    elif reserved == "unknown":
        notes.append("Reservation not stated in the notice.")

    nca = ext.get("nca_category")
    if nca:
        mine = p.get("nca_category")
        if not mine:
            ok = False
            notes.append(f"Needs NCA {nca} or better; profile has no NCA registration.")
        elif mine <= nca:  # NCA 1 is the highest grade
            notes.append(f"Needs NCA {nca} or better: you have NCA {mine}.")
        else:
            ok = False
            notes.append(f"Needs NCA {nca} or better: you have NCA {mine}.")

    sec, cap = ext.get("bid_security_kes"), p.get("max_bid_security_kes")
    if sec and cap and sec > cap:
        ok = False
        notes.append(f"Bid security KES {sec:,.0f} is above your limit of KES {cap:,.0f}.")
    elif sec:
        notes.append(f"Bid security KES {sec:,.0f}.")
    return ok, notes


# ---------- 5. LLM ranking ----------

RANK_SYSTEM = """You help a Kenyan small business decide which government tenders to bid for.
Given the business profile and candidate tenders, score each tender's fit from 0 to 10
(10 = squarely what this business does; 0 = unrelated) and give a reason of at most 15 words.
Judge only by the business's line of work and the tender's scope. Eligibility is checked separately.
Reply with JSON: {"scores": [{"id": <tender id>, "score": <0-10>, "reason": "<text>"}]}"""


def llm_rank(cands, p):
    lines = []
    for c in cands:
        ext = c.get("extraction") or {}
        lines.append(f"- id {c['id']}: {c['title']} | {c.get('category')} | {c.get('entity')} ({c.get('county') or '?'})"
                     + (f" | {ext['summary']}" if ext.get("summary") else ""))
    user = f"BUSINESS: {p['name']}\n{p.get('about', '')}\nBased in/serves: {', '.join(p['counties']) or 'anywhere'}\n\nTENDERS:\n" + "\n".join(lines)
    out = llm.chat_json(RANK_SYSTEM, user)
    scores = {}
    for s in out.get("scores", []):
        try:
            scores[int(s["id"])] = (max(0.0, min(10.0, float(s["score"]))), str(s.get("reason", ""))[:160])
        except (KeyError, TypeError, ValueError):
            continue
    return scores


# ---------- pipeline ----------

WORKERS = 6  # parallel notice downloads; the portal is slow. Groq allows ~30 req/min and the client retries 429s


def extract_many(tenders, db=None, progress=None):
    """Extract details for many tenders: cache hits first, the rest in parallel threads.
    All database reads/writes stay on this thread (sqlite connections aren't thread-safe)."""
    from tools import store
    from tools.extract_notice import cache_key, cacheable, extract_uncached

    out = [None] * len(tenders)
    todo = []
    for i, t in enumerate(tenders):
        key = cache_key(t)
        cached = store.get_extraction(db, key) if (db is not None and key) else None
        if cached:
            out[i] = cached
        else:
            todo.append(i)
    done = len(tenders) - len(todo)
    with ThreadPoolExecutor(WORKERS) as pool:
        futures = {pool.submit(extract_uncached, tenders[i]): i for i in todo}
        for f in as_completed(futures):
            i = futures[f]
            out[i] = f.result()
            key = cache_key(tenders[i])
            if db is not None and key and cacheable(out[i]):
                store.save_extraction(db, key, out[i])
            done += 1
            if progress:
                progress(done, len(tenders))
    # One calm, serial retry for transient failures (slow portal, LLM rate limit).
    for i, r in enumerate(out):
        if r and r.get("status") == "failed":
            out[i] = extract_uncached(tenders[i])
            key = cache_key(tenders[i])
            if db is not None and key and cacheable(out[i]):
                store.save_extraction(db, key, out[i])
    return out

def match(tenders, profile, db=None, use_llm=True, top=10, today=None, extractor=None, ranker=None, progress=None, rank=True):
    """Return ranked matches: tender dict + extraction, eligible, notes, score, reason."""
    p = load_profile(profile)
    today = today or datetime.now()
    ranker = ranker or llm_rank

    cands = prefilter([t for t in tenders if passes_rules(t, p, today)], p)
    if not use_llm:
        results = [None] * len(cands)
    elif extractor:  # injected in tests
        results = [extractor(c) for c in cands]
    else:
        results = extract_many(cands, db, progress)
    for c, ext in zip(cands, results):
        c["extraction"] = ext
        c["eligible"], c["notes"] = eligibility(ext, p)
    eligible = [c for c in cands if c["eligible"]]

    scores = ranker(eligible, p) if (use_llm and rank and eligible) else {}
    for c in eligible:
        default = min(10.0, c["keyword_score"])  # without the LLM, fall back to keyword relevance
        c["score"], c["reason"] = scores.get(c["id"], (default, "Keyword match on the title."))
        if p["counties"] and c.get("county") in p["counties"]:
            c["score"] = min(10.0, c["score"] + 0.5)  # small boost for local tenders
    eligible.sort(key=lambda c: (-c["score"], c["close_at"]))
    return [c for c in eligible if c["score"] >= 5 or not use_llm][:top]


def main():
    from tools import store

    ap = argparse.ArgumentParser()
    ap.add_argument("profile")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--no-llm", action="store_true")
    a = ap.parse_args()
    db = store.connect()
    res = match(store.open_tenders(db), a.profile, db=db, use_llm=not a.no_llm, top=a.top,
                progress=lambda i, n: print(f"  reading notice {i}/{n}", end="\r"))
    print()
    for c in res:
        print(f"{c['score']:4.1f}  {c['title'][:70]}  [{c.get('county')}] closes {c['close_at'][:10]}")
        print(f"      {c['reason']}")
        for n in c["notes"]:
            print(f"      - {n}")


if __name__ == "__main__":
    main()
