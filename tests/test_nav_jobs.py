import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from norway_company_agent.batch import terminal_envelope  # noqa: E402
from norway_company_agent.contract import to_contract  # noqa: E402
from norway_company_agent.evidence import evidence  # noqa: E402
from norway_company_agent.nav_jobs import JobFeed, fetch_jobs, norm, tokens  # noqa: E402

ORG, SUB, OTHER = "995256339", "995324865", "911111111"


class FakeFeed(JobFeed):
    def __init__(self, ads, details, parents=None):
        super().__init__()
        self.ads = ads
        for i, ad in enumerate(ads):
            self._exact.setdefault(norm(ad["businessName"]), []).append(i)
            for t in tokens(ad["businessName"]):
                self._by_token.setdefault(t, set()).add(i)
        self._fake_details = details
        self._fake_parents = parents or {}
        self.retrieved_at = "2026-10-19T12:00:00Z"
        self.ready.set()

    def detail(self, uuid):
        body = self._fake_details.get(uuid)
        return (body, "a" * 64, "2026-10-19T12:00:01Z") if body else (None, None, None)

    def parent_of(self, org):
        return self._fake_parents.get(org)


def ad(uuid, business):
    return {"uuid": uuid, "status": "ACTIVE", "businessName": business, "title": "t"}


def detail(uuid, orgnr, title="Personlig assistent", expires="2099-01-01T00:00:00+01:00"):
    return {"ad_content": {"uuid": uuid, "title": title, "published": "2026-10-01T00:00:00+02:00", "expires": expires,
                           "link": f"https://arbeidsplassen.nav.no/stillinger/stilling/{uuid}",
                           "employer": {"name": "JAG Assistanse AS", "orgnr": orgnr}, "workLocations": [{"city": "HORTEN"}]}}


def profile(locations=None, status="available"):
    return {"organisation_number": ORG, "name": "JAG ASSISTANSE AS",
            "evidence": {"locations": evidence("locations", status, "official_subunits", "https://data.brreg.no",
                                               value={"locations": locations if locations is not None else
                                                      [{"organisation_number": SUB, "name": "JAG ASSISTANSE AS AVD HORTEN"}]})}}


def test_token_rules_ignore_generic_words():
    assert norm("JAG Assistanse AS") == "JAG ASSISTANSE"
    assert tokens("Norsk Barnehage Service AS") == set()


def test_subunit_ad_is_published_with_its_own_evidence():
    feed = FakeFeed([ad("u1", "JAG Assistanse AS")], {"u1": detail("u1", SUB)})
    rec = fetch_jobs(profile(), feed)
    assert rec["status"] == "available"
    job = rec["value"]["job_postings"][0]
    assert job["employer_organisation_number"] == SUB and job["employer_link"] == "sub-unit"
    assert job["url"].endswith("/u1") and "employer.orgnr=995324865" in job["span"]


def test_similar_name_other_employer_is_rejected():
    feed = FakeFeed([ad("u2", "JAG Assistanse AS")], {"u2": detail("u2", OTHER)})
    assert fetch_jobs(profile(), feed)["status"] == "not_found"


def test_expired_ad_is_not_published():
    feed = FakeFeed([ad("u3", "JAG Assistanse AS")], {"u3": detail("u3", SUB, expires="2020-01-01T00:00:00+01:00")})
    assert fetch_jobs(profile(), feed)["status"] == "not_found"


def test_unknown_subunit_needs_brreg_parent_when_list_incomplete():
    big = [{"organisation_number": str(800000000 + i), "name": f"X {i}"} for i in range(1000)]
    feed = FakeFeed([ad("u4", "JAG Assistanse AS")], {"u4": detail("u4", OTHER)}, parents={OTHER: ORG})
    assert fetch_jobs(profile(big), feed)["status"] == "available"
    # with a complete sub-unit list, a number outside it is never looked up or accepted
    assert fetch_jobs(profile(), feed)["status"] == "not_found"


def test_feed_failure_is_source_error():
    feed = FakeFeed([], {})
    feed.error = "TimeoutError: x"
    assert fetch_jobs(profile(), feed)["status"] == "source_error"


def test_contract_claims_cite_each_nav_ad():
    feed = FakeFeed([ad("u1", "JAG Assistanse AS"), ad("u5", "Jag Assistanse Horten")],
                    {"u1": detail("u1", SUB), "u5": detail("u5", ORG, title="Koordinator")})
    pr = profile()
    pr["evidence"]["nav_jobs"] = fetch_jobs(pr, feed)
    env = terminal_envelope(pr, run_id="t", modules=["locations", "nav_jobs"], started_at="s", completed_at="c")
    to_contract(env)
    claims = env["claims"]
    listed = [c for c in claims if c["field"] == "job_postings"][0]
    assert listed["availability"] == "available" and len(listed["value"]) == 2
    atomic = [c for c in claims if c["field"] == "job_posting"]
    assert len(atomic) == 2 and all(len(c["evidence_ids"]) == 1 for c in atomic)
    ev = {e["id"]: e for e in env["evidence"]}
    for c in atomic:
        e = ev[c["evidence_ids"][0]]
        assert e["source_class"] == "official_job_feed" and e["source_url"].startswith("https://arbeidsplassen.nav.no/")
        assert "span" not in c["value"] and e["claim_span"].startswith("NAV feedentry")


def test_observations_follow_starter_schema():
    from norway_company_agent.external_footprint import validate_observation

    feed = FakeFeed([ad("u1", "JAG Assistanse AS")], {"u1": detail("u1", SUB)})
    pr = profile()
    pr["employees"] = 12
    pr["evidence"]["nav_jobs"] = fetch_jobs(pr, feed)
    pr["evidence"]["registry_live"] = evidence("registry_live", "available", "official_registry", "https://data.brreg.no/enhetsregisteret/api/enheter/" + ORG,
                                               value={}, content_sha256="b" * 64)
    env = terminal_envelope(pr, run_id="t", modules=["locations", "nav_jobs", "registry_live"], started_at="s", completed_at="c")
    to_contract(env)
    obs = env["external_observations"]
    assert {(o["platform"], o["signal_type"]) for o in obs} == {("job_board", "job_posting"), ("brreg", "workforce_snapshot")}
    assert all(validate_observation(o) == [] for o in obs)
