from norway_company_agent.public_activity import registry_update_item

ORG = "985589003"


def test_change_entry_names_changed_fields_and_simple_values_only():
    item = registry_update_item({"organisasjonsnummer": ORG, "dato": "2026-05-30T14:23:02.246+02:00", "endringstype": "Endring", "oppdateringsid": 24506622,
                                 "endringer": [{"op": "replace", "path": "/sisteInnsendteAarsregnskap", "value": "2025"},
                                               {"op": "replace", "path": "/forretningsadresse/postnummer", "value": "6893"},
                                               {"op": "replace", "path": "/erIKonsern", "value": True}]})
    assert item["organisation_number"] == ORG and item["date"] == "2026-05-30" and item["kind"] == "registry_update"
    assert item["title"] == "Entity Register record changed: latest filed annual accounts (2025), business address, group membership"
    assert ORG in item["url"] and "24506622" in item["span"] and item["row"] == "24506622"


def test_new_and_unknown_entries_get_plain_titles():
    new = registry_update_item({"organisasjonsnummer": ORG, "dato": "2019-01-02T10:00:00.000Z", "endringstype": "Ny", "oppdateringsid": 1})
    unknown = registry_update_item({"organisasjonsnummer": ORG, "dato": "2019-01-02T10:00:00.000Z", "endringstype": "Ukjent", "oppdateringsid": 2})
    assert new["title"] == "Registered in the Entity Register" and unknown["title"] == "Entity Register record updated"


def test_entries_without_number_date_or_id_are_dropped():
    assert registry_update_item({"organisasjonsnummer": "123", "dato": "2019-01-02T10:00:00.000Z", "oppdateringsid": 1}) is None
    assert registry_update_item({"organisasjonsnummer": ORG, "dato": "", "oppdateringsid": 1}) is None
    assert registry_update_item({"organisasjonsnummer": ORG, "dato": "2019-01-02T10:00:00.000Z"}) is None


def test_batch_load_keeps_only_requested_companies_and_feeds_fetch_activity():
    from norway_company_agent import public_activity as pa
    feed = {"page": {"totalPages": 1}, "_embedded": {"oppdaterteEnheter": [
        {"organisasjonsnummer": ORG, "dato": "2026-05-30T12:23:02.246Z", "endringstype": "Endring", "oppdateringsid": 7,
         "endringer": [{"op": "replace", "path": "/navn", "value": "NYTT NAVN AS"}]},
        {"organisasjonsnummer": "111111111", "dato": "2026-05-30T12:23:02.246Z", "endringstype": "Endring", "oppdateringsid": 8}]}}
    p = pa.PublicActivity()
    p._get = lambda url: __import__("json").dumps(feed).encode()
    p._load_brreg_updates([ORG])
    assert list(p.brreg_updates) == [ORG] and p.requests == 0  # the stub replaces the counted request
    rec = pa.fetch_activity({"organisation_number": ORG}, p)
    assert rec["status"] == "available" and rec["value"]["items"][0]["title"] == "Entity Register record changed: name (NYTT NAVN AS)"
    assert rec["value"]["items"][0]["content_sha256"]
