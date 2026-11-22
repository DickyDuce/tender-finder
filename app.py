"""Kenya Tender Finder: Streamlit UI.  Run: streamlit run app.py"""

import os
import threading
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

# Streamlit Community Cloud stores secrets in st.secrets; mirror them into env for the tools.
try:
    for key in ("GROQ_API_KEY", "LLM_PROVIDER", "LLM_MODEL"):
        if key in st.secrets and not os.getenv(key):
            os.environ[key] = st.secrets[key]
except Exception:  # no secrets file locally
    pass

from concurrent.futures import ThreadPoolExecutor  # noqa: E402

import requests  # noqa: E402

from tools import digest, fetch_tenders, llm, store  # noqa: E402
from tools.extract_notice import cache_key, cacheable, extract_uncached  # noqa: E402
from tools.match import WORKERS, load_profile, match, passes_rules, prefilter  # noqa: E402
from tools.warm_cache import CACHE_URL  # noqa: E402

PROFILES = {p.stem: load_profile(p) for p in sorted((Path(__file__).parent / "profiles").glob("*.yaml"))}
CATEGORIES = ["Works", "Goods", "Non Consultancy Services", "Consultancy Services"]
AGPO = {"None": None, "Youth": "youth", "Women": "women", "Persons with disabilities": "pwd"}
BADGE = {"youth": "🧑 Youth only", "women": "👩 Women only", "pwd": "♿ PWD only", "agpo_any": "AGPO groups only",
         "open": "✅ Open to all", "unknown": "❔ Reservation not stated"}

st.set_page_config(page_title="Kenya Tender Finder", page_icon="📢", layout="wide")


@st.cache_resource
def db_and_lock():
    db = store.connect(shared=True)
    # Seed with notices pre-read by the scheduled warm-cache job, so searches don't start cold.
    try:
        r = requests.get(CACHE_URL, timeout=15)
        if r.ok:
            store.import_extractions(db, r.json().get("extractions", {}))
    except Exception:  # cache is an optimisation only
        pass
    return db, threading.Lock()


@st.cache_data(ttl=6 * 3600, show_spinner="Fetching active tenders from tenders.go.ke…")
def load_tenders():
    tenders = fetch_tenders.fetch_active()
    db, lock = db_and_lock()
    with lock:
        store.upsert_tenders(db, tenders)
    return tenders, datetime.now()


def _warm(tenders):
    """Background: read notices the sample profiles will need, before anyone clicks.
    Downloads run without the lock; the lock is held only for quick cache reads/writes."""
    db, lock = db_and_lock()
    today = datetime.now()
    todo = {}
    for p in PROFILES.values():
        for t in prefilter([t for t in tenders if passes_rules(t, p, today)], p):
            key = cache_key(t)
            with lock:
                cached = store.get_extraction(db, key) if key else True
            if not cached:
                todo[key] = t

    def one(t):
        result = extract_uncached(t)
        if cacheable(result):
            with lock:
                store.save_extraction(db, cache_key(t), result)

    with ThreadPoolExecutor(WORKERS) as pool:
        list(pool.map(one, todo.values()))


@st.cache_resource
def start_warmup(_tenders):
    if llm.is_configured():
        threading.Thread(target=_warm, args=(_tenders,), daemon=True).start()
    return True


# ---------- sidebar: business profile ----------
with st.sidebar:
    st.header("Your business")
    names = {k: p["name"] for k, p in PROFILES.items()}
    choice = st.radio("Profile", [*names, "custom"], format_func=lambda k: names.get(k, "✏️ Describe my own business"))
    base = PROFILES.get(choice) or PROFILES[next(iter(PROFILES))]
    if choice == "custom":
        about = st.text_area("What does your business do?", "Supplies and delivers ...")
        kw = st.text_input("Keywords (comma-separated)", ", ".join(base["keywords"][:8]))
        cats = st.multiselect("Tender categories", CATEGORIES, base["categories"])
        counties = st.text_input("Counties you serve (comma-separated, blank = anywhere)", "Nairobi")
        agpo = st.selectbox("AGPO certificate", list(AGPO))
        nca = st.selectbox("NCA category (contractors)", ["None", *range(1, 9)])
        cap = st.number_input("Max bid security you can raise (KES)", 0, 50_000_000, 200_000, step=50_000)
        profile = load_profile({
            "id": "custom", "name": "Your business", "about": about,
            "keywords": [k.strip() for k in kw.split(",") if k.strip()], "categories": cats,
            "counties": [c.strip() for c in counties.split(",") if c.strip()], "county_strict": False,
            "agpo_group": AGPO[agpo], "nca_category": None if nca == "None" else int(nca),
            "max_bid_security_kes": cap or None,
        })
    else:
        profile = base
        st.caption(profile.get("about", ""))
        st.caption(
            f"**AGPO:** {profile['agpo_group'] or 'none'} · **NCA:** {profile['nca_category'] or 'n/a'} · "
            f"**Serves:** {', '.join(profile['counties']) or 'anywhere'}"
        )
        st.caption("Sample business: synthetic, not a real company.")
    st.divider()
    st.caption(f"AI provider: {llm.provider_name()} {'✅' if llm.is_configured() else '⚠️ not configured'}")

# ---------- header ----------
st.title("📢 Kenya Tender Finder")
st.caption("Finds government tenders your business can actually bid for: live data from the Public Procurement "
           "Information Portal (tenders.go.ke), eligibility read from the tender notice by AI and checked by rules.")

try:
    tenders, fetched_at = load_tenders()
except Exception as e:
    st.error(f"Could not reach tenders.go.ke right now ({e}). Try again in a few minutes.")
    st.stop()

start_warmup([t for t in tenders if not fetch_tenders.past_bidding(t)])
today = datetime.now()
open_t = [t for t in tenders if t["close_at"] >= today.strftime("%Y-%m-%d")]
c1, c2, c3 = st.columns(3)
c1.metric("Active tenders", len(open_t))
c2.metric("Closing within 7 days", sum(digest.days_left(t["close_at"], today) <= 7 for t in open_t))
c3.metric("Already past bidding", sum(fetch_tenders.past_bidding(t) for t in open_t),
          help="Still listed as active, but the portal already has opening minutes, evaluation or award documents.")

tab_match, tab_browse, tab_how = st.tabs(["🎯 Matches for you", "🔎 Browse all", "⚙️ How it works"])

# ---------- matches ----------
with tab_match:
    if not llm.is_configured():
        st.info("Add a free `GROQ_API_KEY` to `.env` to read tender notices with AI.")
    st.write(f"Matching **{profile['name']}** against {len(open_t)} active tenders.")
    key = f"matches::{profile['id']}::{hash(str(sorted(profile.items(), key=str)))}"
    if st.button("Find my tenders", type="primary", disabled=not llm.is_configured()):
        db, lock = db_and_lock()
        bar = st.progress(0.0, "Reading tender notices…")
        with lock:
            st.session_state[key] = match(
                open_t, profile, db=db, top=12,
                progress=lambda i, n: bar.progress(i / n, f"Reading tender notice {i}/{n}…"),
            )
        bar.empty()
    results = st.session_state.get(key)
    if results is None:
        st.caption("Rules and keywords pick up to 25 candidates, AI reads each notice (cached after the first read), "
                   "rules check eligibility, then AI ranks the fit. The first run takes a minute or two.")
    elif not results:
        st.warning("No eligible matches right now. Try more keywords or more categories.")
    for m in results or []:
        ext = m.get("extraction") or {}
        with st.container(border=True):
            left, right = st.columns([5, 1])
            left.markdown(f"**{m['title']}**  \n🏛 {m.get('entity')} · {m.get('county') or 'county ?'} · {m.get('category')}")
            right.metric("Fit", f"{m['score']:.0f}/10")
            st.markdown(f"⏰ Closes **{digest.deadline_text(m['close_at'], today)}** · "
                        f"{BADGE.get(ext.get('reserved_for'), '❔ Notice not readable')}"
                        + (f" · NCA {ext['nca_category']}" if ext.get("nca_category") else ""))
            st.markdown(f"💡 {m['reason']}")
            if ext.get("summary"):
                st.caption(ext["summary"])
            for n in m["notes"]:
                st.caption(f"• {n}")
            if ext.get("required_documents"):
                st.caption("📋 Required: " + "; ".join(ext["required_documents"]))
            st.markdown(f"[📄 Open tender document]({digest.doc_link(m)}) · Ref `{m['tender_ref']}`")

# ---------- browse ----------
with tab_browse:
    df = pd.DataFrame(open_t)
    df["days_left"] = df["close_at"].map(lambda c: digest.days_left(c, today))
    df["past_bidding"] = [fetch_tenders.past_bidding(t) for t in open_t]
    df["document"] = [t["documents"][0]["url"] if t["documents"] else None for t in open_t]
    f1, f2, f3 = st.columns([2, 2, 3])
    cat = f1.multiselect("Category", sorted(df["category"].dropna().unique()))
    cty = f2.multiselect("County", sorted(df["county"].dropna().unique()))
    q = f3.text_input("Search title or entity")
    view = df[~df["past_bidding"]]
    if cat:
        view = view[view["category"].isin(cat)]
    if cty:
        view = view[view["county"].isin(cty)]
    if q:
        view = view[view["title"].str.contains(q, case=False) | view["entity"].str.contains(q, case=False)]
    st.dataframe(
        view.sort_values("close_at")[["title", "entity", "county", "category", "method", "close_at", "days_left", "tender_ref", "document"]],
        hide_index=True, width="stretch",
        column_config={"document": st.column_config.LinkColumn("Document", display_text="PDF"),
                       "title": st.column_config.TextColumn("Tender", width="large"),
                       "entity": "Procuring entity", "county": "County", "category": "Category",
                       "method": "Method", "close_at": "Closes", "days_left": "Days left", "tender_ref": "Reference"},
    )
    st.caption(f"{len(view)} tenders · data fetched {fetched_at:%d %b %Y %H:%M} · refreshed every 6 hours")

# ---------- how ----------
with tab_how:
    st.markdown("""
1. **Fetch:** the portal's public JSON feed, about 500 active tenders. Only an allow-list of tender fields is kept;
   the feed also contains officers' personal details, which this app never stores or shows.
2. **Rules:** drop tenders that close too soon, are in the wrong category or county, or are already past bidding
   (award or evaluation documents are posted).
3. **Prefilter:** BM25 keyword relevance picks the top 25 candidates, so the free AI quota is spent only where it matters.
4. **Read the notice:** the AI fills a fixed schema (reservation, NCA category, bid security, required documents).
   A reservation claim must quote the notice word for word; if the quote isn't in the PDF, it's discarded.
5. **Eligibility:** plain Python compares the notice against your AGPO group, NCA category and budget.
6. **Rank:** the AI scores fit from 0 to 10 with a one-line reason.

The same pipeline can optionally run every morning on GitHub Actions and send new matches to a Telegram bot.
**Always confirm requirements in the official tender document before bidding.**
""")
