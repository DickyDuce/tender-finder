"""Offline tests: no network, no LLM calls."""

import json
from datetime import datetime

import pytest

from tests.fixtures import COUNTIES, NOTICE_TEXT, PROFILE, extraction, raw_tender, tender
from tools import digest, store
from tools.extract_notice import candidate_documents, focus, validate
from tools.fetch_tenders import FIELDS, clean, past_bidding
from tools.llm import _parse_json
from tools.match import bm25_scores, eligibility, match, passes_rules, tokens

TODAY = datetime(2026, 11, 21)


# ---------- privacy: the allow-list ----------

def test_clean_keeps_only_allowlisted_fields():
    out = clean(raw_tender(), COUNTIES)
    assert tuple(out) == FIELDS
    assert out["county"] == "Kiambu"
    assert out["entity_type"] == "County Government"
    assert out["title"] == "Proposed construction of 2No. classrooms at Sample Primary School"  # whitespace normalised
    assert out["documents"] == [{"type": "Tender Notice", "url": "https://tenders.go.ke/storage/Documents/1-tender-notice.pdf"}]


def test_no_personal_data_survives():
    blob = json.dumps(clean(raw_tender(), COUNTIES))
    for secret in ("00000000", "+254 700 000000", "jane.private@example.com", "FAKE-TOKEN-123",
                   "JANE TESTER", "0711111111", "0700000000", "id_number", "created_by", "team_members"):
        assert secret not in blob


def test_store_holds_only_clean_records(tmp_path):
    db = store.connect(tmp_path / "t.db")
    store.upsert_tenders(db, [clean(raw_tender(), COUNTIES)])
    dump = "\n".join(db.iterdump())
    assert "00000000" not in dump and "FAKE-TOKEN" not in dump


# ---------- store ----------

def test_upsert_reports_only_new_ids(tmp_path):
    db = store.connect(tmp_path / "t.db")
    assert store.upsert_tenders(db, [tender(1, "a"), tender(2, "b")]) == [1, 2]
    assert store.upsert_tenders(db, [tender(2, "b"), tender(3, "c")]) == [3]
    assert [t["id"] for t in store.open_tenders(db, TODAY)] == [1, 2, 3]
    store.mark_sent(db, "p", [1])
    assert store.already_sent(db, "p") == {1}


def test_closed_tenders_are_not_open(tmp_path):
    db = store.connect(tmp_path / "t.db")
    store.upsert_tenders(db, [tender(1, "old", close="2026-11-01 10:00:00"), tender(2, "new")])
    assert [t["id"] for t in store.open_tenders(db, TODAY)] == [2]


# ---------- extraction validation (grounding) ----------

def test_validate_keeps_grounded_reservation():
    out = validate({"reserved_for": "women", "reserved_evidence": "Women NCA 6 and above",
                    "nca_category": "6", "bid_security_kes": "KES 50,000"}, NOTICE_TEXT)
    assert out["reserved_for"] == "women"
    assert out["nca_category"] == 6
    assert out["bid_security_kes"] == 50000.0


def test_validate_drops_reservation_with_invented_quote():
    out = validate({"reserved_for": "youth", "reserved_evidence": "Reserved for youth only"}, NOTICE_TEXT)
    assert out["reserved_for"] == "unknown" and out["reserved_evidence"] is None


@pytest.mark.parametrize("raw,expected", [
    ({"reserved_for": "banana"}, "unknown"),
    ({"reserved_for": None}, "unknown"),
    ({"reserved_for": "OPEN"}, "open"),
])
def test_validate_normalises_reserved(raw, expected):
    assert validate(raw, NOTICE_TEXT)["reserved_for"] == expected


def test_validate_rejects_nonsense_numbers():
    out = validate({"nca_category": 42, "bid_security_kes": "nil", "required_documents": "Tax compliance"}, NOTICE_TEXT)
    assert out["nca_category"] is None
    assert out["bid_security_kes"] is None
    assert out["required_documents"] == ["Tax compliance"]


def test_candidate_documents_prefers_notice_and_caps_downloads():
    t = tender(1, "x", docs=[
        {"type": "Additional Documents", "url": "a"}, {"type": "Tender Document", "url": "d"},
        {"type": "Tender Notice", "url": "n"}])
    assert [d["url"] for d in candidate_documents(t)] == ["n", "d"]


def test_focus_centres_on_tender_ref():
    text = "x" * 20000 + " REF/77 the part we need " + "y" * 20000
    out = focus(text, {"tender_ref": "REF/77"}, limit=3000)
    assert "REF/77" in out and len(out) == 3000


def test_parse_json_tolerates_fences():
    assert _parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _parse_json('Sure! {"a": 2} hope that helps') == {"a": 2}


# ---------- rules & eligibility ----------

def test_past_bidding_detected_from_documents():
    assert past_bidding(tender(1, "x", docs=[{"type": "Notification of Award", "url": "u"}]))
    assert not past_bidding(tender(1, "x"))


def test_hard_rules():
    from tools.match import load_profile
    p = load_profile(PROFILE)
    assert passes_rules(tender(1, "Construction of classrooms"), p, TODAY)
    assert not passes_rules(tender(2, "Construction of classrooms", close="2026-11-22 10:00:00"), p, TODAY)  # too soon
    assert not passes_rules(tender(3, "Supply of laptops", category="Goods"), p, TODAY)
    assert not passes_rules(tender(4, "Drilling of borehole"), p, TODAY)  # excluded keyword
    assert not passes_rules(tender(5, "Construction", docs=[{"type": "Evaluation Report", "url": "u"}]), p, TODAY)


@pytest.mark.parametrize("ext,ok", [
    (extraction("women", nca=6), True),
    (extraction("women", nca=7), True),      # NCA 6 contractor may bid for NCA 7 work
    (extraction("women", nca=5), False),     # ...but not NCA 5
    (extraction("youth"), False),            # reserved for another group
    (extraction("agpo_any"), True),
    (extraction("open", security=500000), False),  # bid security above the profile's limit
    (extraction("unknown"), True),           # unknown stays in, flagged
    ({"status": "no_text"}, True),
])
def test_eligibility(ext, ok):
    from tools.match import load_profile
    assert eligibility(ext, load_profile(PROFILE))[0] is ok


def test_bm25_ranks_relevant_title_first():
    s = bm25_scores(["construction of classrooms", "supply of drugs", "fencing works"], tokens("classrooms construction"))
    assert s[0] > s[2] >= s[1] == 0


def test_match_pipeline_with_fakes():
    tenders = [
        tender(1, "Construction of 2 classrooms"),
        tender(2, "Fencing of school compound", county="Nairobi"),
        tender(3, "Construction of classrooms (youth)"),
        tender(4, "Supply of drugs"),  # no keyword overlap: never reaches the LLM
    ]
    exts = {1: extraction("women", nca=6), 2: extraction("open"), 3: extraction("youth")}
    seen = []

    def fake_extract(t):
        seen.append(t["id"])
        return exts[t["id"]]

    def fake_rank(cands, p):
        return {1: (8.0, "classrooms"), 2: (9.0, "fencing")}

    out = match(tenders, PROFILE, today=TODAY, extractor=fake_extract, ranker=fake_rank)
    assert 4 not in seen
    assert [m["id"] for m in out] == [2, 1]  # 9.0 vs 8.0 (+0.5 local boost for Kiambu)
    assert out[1]["score"] == 8.5


# ---------- digest ----------

def test_digest_escapes_html_and_links_document():
    m = dict(tender(1, "Supply of <chairs> & tables"), extraction=extraction("women", nca=6), score=9, reason="fits")
    msg = digest.build_messages("Test & Co", [m], today=TODAY)[0]
    assert "&lt;chairs&gt; &amp; tables" in msg and "Test &amp; Co" in msg
    assert 'href="https://x/doc.pdf"' in msg
    assert "in 19 days" in msg and "Women (AGPO)" in msg and "NCA 6" in msg


def test_digest_splits_long_lists():
    ms = [dict(tender(i, "Construction " * 12), extraction=extraction(), score=7, reason="r" * 100) for i in range(40)]
    msgs = digest.build_messages("P", ms, today=TODAY)
    assert len(msgs) > 1 and all(len(x) <= 4096 for x in msgs)


def test_digest_empty():
    assert "No new matching tenders" in digest.build_messages("P", [], today=TODAY)[0]


def test_grounding_accepts_fragments_split_by_table_columns():
    text = "3 UOK/853/0145 35398 Reserved Monday 30th\nfor Women November, 2026\nSupply of Medical Equipment"
    out = validate({"reserved_for": "women", "reserved_evidence": "Reserved ... for Women"}, text)
    assert out["reserved_for"] == "women"


def test_grounding_rejects_fragments_far_apart_or_missing():
    far = "Reserved " + "x " * 400 + " for Women"
    assert validate({"reserved_for": "women", "reserved_evidence": "Reserved ... for Women"}, far)["reserved_for"] == "unknown"
    assert validate({"reserved_for": "youth", "reserved_evidence": "Reserved ... for Youth"}, NOTICE_TEXT)["reserved_for"] == "unknown"


def test_fetch_retries_after_rate_limit():
    from tools import fetch_tenders

    class Resp:
        def __init__(self, status, headers=None, body=None):
            self.status_code, self.headers, self.body = status, headers or {}, body

        def raise_for_status(self):
            if self.status_code >= 400:
                import requests
                raise requests.HTTPError(f"{self.status_code}")

        def json(self):
            return self.body

    replies = [Resp(429, {"Retry-After": "3"}), Resp(200, body={"ok": 1})]

    class Session:
        def get(self, *a, **k):
            return replies.pop(0)

    waits = []
    assert fetch_tenders._get("u", session=Session(), sleep=waits.append) == {"ok": 1}
    assert waits == [5]  # max(Retry-After + 1, 5)


def test_cache_export_import_roundtrip(tmp_path):
    a = store.connect(tmp_path / "a.db")
    store.save_extraction(a, "u1", extraction("women"))
    store.save_extraction(a, "u2", extraction("open"))
    data = store.export_extractions(a, {"u1", "missing"})
    assert list(data) == ["u1"]

    b = store.connect(tmp_path / "b.db")
    store.save_extraction(b, "u1", extraction("youth"))  # local entry must win
    assert store.import_extractions(b, {**data, "u3": extraction("pwd"), "bad": "x"}) == 1
    assert store.get_extraction(b, "u1")["reserved_for"] == "youth"
    assert store.get_extraction(b, "u3")["reserved_for"] == "pwd"
    assert store.get_extraction(b, "bad") is None
