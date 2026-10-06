import unittest
from unittest import mock

from norway_company_agent import footprint_plus
from norway_company_agent.contract import to_contract


def _profile():
    return {"organisation_number": "985589003", "name": "TEST AS", "evidence": {"registry": {"value": {}}}}


def _proof(pages):
    def fake_get(url, *a, **k):
        body = pages.get(url)
        return (body.encode() if body else None), url
    with mock.patch.object(footprint_plus, "_get", fake_get):
        return footprint_plus.hard_identity_proof(_profile(), {"source_url": "https://example.no/"})[0]


class HardProofTests(unittest.TestCase):
    def test_org_number_on_linked_privacy_page(self):
        pages = {"https://example.no/": '<a href="/handel-og-personvern">Personvern</a>', "https://example.no/handel-og-personvern": "Org.nr 985 589 003 MVA"}
        self.assertIn("exact organisation number", _proof(pages) or "")

    def test_org_number_inside_longer_digit_string_is_not_proof(self):
        self.assertIsNone(_proof({"https://example.no/": "Ring 47985589003 eller 9855890031"}))

    def test_other_companys_number_is_not_proof(self):
        self.assertIsNone(_proof({"https://example.no/": "Org.nr 912 345 678"}))


    def test_street_address_and_postcode_from_live_registry_is_proof(self):
        profile = {
            "organisation_number": "985589003",
            "name": "TEST AS",
            "evidence": {
                "registry_live": {
                    "value": {
                        "business_address": {
                            "adresse": ["Storgata 15B"],
                            "postnummer": "0182",
                            "poststed": "Oslo",
                        }
                    }
                }
            },
        }
        pages = {"https://example.no/": "Besøk oss i Storgata 15B, 0182 Oslo"}
        with mock.patch.object(footprint_plus, "_get", lambda url, *a, **k: ((pages.get(url).encode() if pages.get(url) else None), url)):
            proof, _ = footprint_plus.hard_identity_proof(profile, {"source_url": "https://example.no/"})
        self.assertIn("registered street address and postcode", proof or "")

    def test_phone_or_leader_alone_is_not_proof(self):
        profile = {
            "organisation_number": "985589003",
            "name": "TEST AS",
            "evidence": {
                "registry_live": {"value": {"phone": "22334455"}},
                "roles": {"value": {"roles": [{"name": "Ola Nordmann", "role_code": "DAGL"}]}},
            },
        }
        pages = {"https://example.no/": "Kontakt daglig leder Ola Nordmann på tlf 22334455"}
        with mock.patch.object(footprint_plus, "_get", lambda url, *a, **k: ((pages.get(url).encode() if pages.get(url) else None), url)):
            proof, _ = footprint_plus.hard_identity_proof(profile, {"source_url": "https://example.no/"})
        self.assertIsNone(proof)


if __name__ == "__main__":
    unittest.main()
