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


if __name__ == "__main__":
    unittest.main()
