"""Synthetic test data. Every name, number and email here is made up."""

FAKE_OFFICER = {
    "id": 99, "name": "JANE TESTER", "username": "jane@example.go.ke", "id_number": "00000000",
    "phone": "+254 700 000000", "alt_email": "jane.private@example.com", "email": "jane@example.go.ke",
    "validation_token": "FAKE-TOKEN-123",
    "pe": {"id": 1, "name": "SAMPLE COUNTY GOVERNMENT", "type": {"id": 5, "description": "County Government"}},
}


def raw_tender(**over):
    rec = {
        "id": 1001, "ocid": "ocds-x-1001", "tender_ref": "SCG/T/001/2026-2027",
        "title": "Proposed  construction of 2No. classrooms at Sample Primary School",
        "published_at": "2026-11-10 00:00:00", "close_at": "2026-12-10 10:00:00",
        "tender_fee": 0, "bid_security_value": None, "description": None, "is_agpo": None,
        "addendum_added": 0,
        "pe": {"id": 1, "name": "SAMPLE COUNTY GOVERNMENT", "county_id": 5, "telephone": "0700000000"},
        "procurement_category": {"id": 2, "title": "Works"},
        "procurement_method": {"id": 1, "title": "Open Tender"},
        "submission_methods": [{"id": 9, "title": "Electronic submission"}],
        "documents": [{"url": "/storage/Documents/1-tender-notice.pdf", "type": {"description": "Tender Notice"}}],
        "created_by": FAKE_OFFICER,
        "team_members": [FAKE_OFFICER],
        "bidders": [{"name": "Some Bidder Ltd", "phone": "0711111111"}],
    }
    rec.update(over)
    return rec


COUNTIES = {1: "Nairobi", 5: "Kiambu"}

NOTICE_TEXT = """SAMPLE CONSTITUENCY NG-CDF
TENDER INVITATION NOTICE
S.No. Tender No Name of the Project Category NCA
1. SCG/T/001/2026-2027 PROPOSED CONSTRUCTION OF 2NO CLASSROOMS AT SAMPLE PRIMARY SCHOOL Women NCA 6 and above
2. SCG/T/002/2026-2027 PROPOSED FENCING AT SAMPLE SECONDARY SCHOOL All NCA 7 and above
Bidders MUST provide: valid tax compliance certificate, NCA registration, AGPO certificate.
Site visit: 20th November 2026 at 9:00 am at the respective sites.
"""


def tender(id_, title, close="2026-12-10 10:00:00", category="Works", county="Kiambu", docs=None):
    return {
        "id": id_, "ocid": f"o-{id_}", "tender_ref": f"REF/{id_}", "title": title,
        "published_at": "2026-11-10 00:00:00", "close_at": close, "tender_fee": 0,
        "bid_security_value": None, "category": category, "method": "Open Tender",
        "entity": "SAMPLE ENTITY", "entity_type": None, "county": county, "addendum": False,
        "submission": "Electronic submission",
        "documents": docs if docs is not None else [{"type": "Tender Notice", "url": f"https://x/{id_}.pdf"}],
    }


def extraction(reserved="open", nca=None, security=None):
    return {"status": "ok", "summary": "s", "reserved_for": reserved, "reserved_evidence": "q",
            "nca_category": nca, "required_documents": [], "bid_security_kes": security,
            "site_visit_or_prebid": None, "location": None, "doc_url": "https://x/doc.pdf", "doc_type": "Tender Notice"}


PROFILE = {
    "id": "test_women", "name": "Test Builders", "about": "Women-owned NCA 6 contractor",
    "keywords": ["construction", "classrooms", "fencing", "renovation"], "categories": ["Works"],
    "counties": ["Kiambu"], "county_strict": False, "agpo_group": "women", "nca_category": 6,
    "max_bid_security_kes": 300000, "min_days_to_close": 2, "exclude_keywords": ["borehole"],
}
