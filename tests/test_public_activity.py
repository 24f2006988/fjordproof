import io
import json
import zipfile

from norway_company_agent import public_activity as pa

ORG = "916340257"


def _xlsx(rows):
    """Minimal .xlsx with inline strings: enough for the stdlib reader."""
    def cell(ref, value):
        return f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>' if isinstance(value, str) else f'<c r="{ref}"><v>{value}</v></c>'
    body = "".join(f'<row r="{n}">' + "".join(cell(f"{chr(65 + i)}{n}", v) for i, v in enumerate(row)) + "</row>"
                   for n, row in enumerate(rows, start=1))
    sheet = ('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>' + body + "</sheetData></worksheet>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


HEAD = ["Søknadsdato", "Prosjektnummer", "Prosjekttittel", "Organisasjonsnr", "Godkjent", "Fra-år", "Til-år", "Vedtaksdato"]
SKF = _xlsx([HEAD,
             [45422, "25", "Snøskred-radar", ORG, "JA", 45292, 46022, 45534],
             [45422, "26", "Avslått prosjekt", ORG, "NEI", 45292, 46022, 45535],
             [45422, "27", "Annen bedrift", "999999999", "JA", 45292, 46022, 45536]])

TED = {"notices": [
    {"publication-number": "433790-2024", "publication-date": "2024-07-19Z", "notice-type": "can-standard",
     "winner-identifier": [ORG, "919 642 564"], "winner-country": ["NOR", "NOR"],
     "buyer-name": {"eng": ["Statens vegvesen"]}, "title-proc": {"eng": "Framework agreement avalanche control equipment"}},
    # Same 9 digits as a Portuguese tax number: not this company
    {"publication-number": "1-2025", "publication-date": "2025-01-02Z", "winner-identifier": ["515000000", ORG],
     "winner-country": ["PRT", "PRT"], "title-proc": {"por": "Obras"}},
]}


def _loaded(ted=TED, skf=SKF):
    p = pa.PublicActivity()
    p._post = lambda url, body: json.dumps(ted).encode()
    p._get = lambda url: skf
    p.load([ORG, "999999999"])
    return p


def test_ted_award_needs_a_norwegian_winner_at_the_same_position():
    items = _loaded().ted[ORG]
    assert [(i["notice"], i["date"], i["buyer"]) for i in items] == [("433790-2024", "2024-07-19", "Statens vegvesen")]
    assert items[0]["url"] == "https://ted.europa.eu/en/notice/-/detail/433790-2024"


def test_skattefunn_rows_need_this_org_number_approval_and_a_decision_date():
    items = _loaded().skattefunn[ORG]
    assert [(i["title"], i["date"], i["period"]) for i in items] == [("Snøskred-radar", "2024-08-30", "2024-2025")]
    assert "Organisasjonsnr 916340257" in items[0]["span"]


def test_fetch_activity_states():
    p = _loaded()
    rec = pa.fetch_activity({"organisation_number": ORG}, p)
    assert rec["status"] == "available"
    assert [i["kind"] for i in rec["value"]["items"]] == ["rd_tax_credit_project", "public_contract_award"]  # newest first
    assert all(i["content_sha256"] and i["retrieved_at"] for i in rec["value"]["items"])
    assert pa.fetch_activity({"organisation_number": "123456789"}, p)["status"] == "not_found"
    broken = pa.PublicActivity()
    broken._post = broken._get = lambda *a: (_ for _ in ()).throw(OSError("down"))
    broken.load([ORG])
    assert pa.fetch_activity({"organisation_number": ORG}, broken)["status"] == "source_error"


def test_skattefunn_column_change_is_an_error_not_a_guess():
    p = _loaded(skf=_xlsx([["Navn", "Tittel"], ["X AS", "Y"]]))
    assert "skattefunn" in p.errors and ORG not in p.skattefunn


def test_contract_claims_observations_and_recent_activity():
    from norway_company_agent.batch import terminal_envelope
    from norway_company_agent.contract import to_contract
    from norway_company_agent.evidence import evidence
    from norway_company_agent.official import normalize_entity

    live = normalize_entity({"organisasjonsnummer": ORG, "navn": "WYSSEN NORGE AS"})
    profile = {"organisation_number": ORG, "name": "WYSSEN NORGE AS", "legal_form": "AS", "bankrupt": False, "liquidating": False,
               "evidence": {"registry_live": evidence("registry_live", "available", "official_registry_live",
                                                      f"https://data.brreg.no/enhetsregisteret/api/enheter/{ORG}", value=live),
                            "public_activity": pa.fetch_activity({"organisation_number": ORG}, _loaded())},
               "run_metrics": {"requests": 1, "latencies_ms": [1]}}
    env = to_contract(terminal_envelope(profile, run_id="t", modules=["registry_live", "public_activity"],
                                        started_at="2026-01-01T00:00:00Z", completed_at="2026-01-01T00:00:01Z"))
    items = [c for c in env["claims"] if c["field"] == "public_activity_item"]
    assert len(items) == 2 and all(c["evidence_ids"] and c["source_path"] for c in items)
    ev = {e["id"]: e for e in env["evidence"]}
    assert any("#row=" in ev[c["evidence_ids"][0]]["source_record"] for c in items)
    assert sum(o["source_class"] == "official_public_activity" for o in env["external_observations"]) == 2
    recent = next(s for s in env["synthesis"]["sections"] if s["label"] == "Recent activity")
    assert "public contract awarded (TED) 2024-07-19 by Statens vegvesen" in recent["text"]
    assert "SkatteFUNN R&D project approved 2024-08-30" in recent["text"]
