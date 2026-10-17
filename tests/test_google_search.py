import os
import unittest
from unittest import mock

from norway_company_agent import google_search as gs

PROFILE = {
    "organisation_number": "990446539", "name": "KJERRINGHOLMEN AS", "municipality": "TØNSBERG",
    "evidence": {"registry_live": {"value": {"business_address": {"postnummer": "3140", "poststed": "NØTTERØY"}}}},
}
KEYS = ("GOOGLE_SEARCH_API_KEY", "GOOGLE_CSE_ID", "GOOGLE_PLACES_API_KEY")


def env(**values):
    clean = {k: "" for k in KEYS}
    clean.update(values)
    return mock.patch.dict(os.environ, clean)


class GoogleSearch(unittest.TestCase):
    def test_no_key_means_no_request_and_no_candidates(self):
        with env(), mock.patch.object(gs, "_get_json") as g, mock.patch.object(gs, "_post_json") as p:
            self.assertEqual(gs.google_candidate_hosts(PROFILE), ([], 0, None))
            g.assert_not_called()
            p.assert_not_called()

    def test_custom_search_filters_directories_and_ranks_name_host_first(self):
        payload = {"items": [
            {"link": "https://www.proff.no/selskap/kjerringholmen-as/990446539", "title": "Kjerringholmen AS", "snippet": "990446539"},
            {"link": "https://www.facebook.com/kjerringholmen", "title": "Kjerringholmen", "snippet": ""},
            {"link": "https://example-blog.no/post", "title": "Om graving", "snippet": ""},
            {"link": "https://www.kjerringholmen.com/kontakt", "title": "Kontakt", "snippet": ""},
        ]}
        with env(GOOGLE_SEARCH_API_KEY="k", GOOGLE_CSE_ID="cx"), mock.patch.object(gs, "_get_json", return_value=payload) as g:
            hosts, requests, provider = gs.google_candidate_hosts(PROFILE)
        self.assertEqual((hosts, requests, provider), (["kjerringholmen.com", "example-blog.no"], 1, "google_custom_search"))
        self.assertIn("990446539", g.call_args[0][0])

    def test_places_prefers_listing_at_company_postcode(self):
        payload = {"places": [
            {"websiteUri": "https://other-kjerringholmen.no/", "formattedAddress": "Storgata 1, 0155 Oslo"},
            {"websiteUri": "https://www.kjerringholmen.com/", "formattedAddress": "Kjerringholmen 5, 3140 Nøtterøy"},
            {"formattedAddress": "no website, 3140 Nøtterøy"},
        ]}
        with env(GOOGLE_PLACES_API_KEY="k"), mock.patch.object(gs, "_post_json", return_value=payload) as p:
            hosts, requests, provider = gs.google_candidate_hosts(PROFILE)
        self.assertEqual(provider, "google_places")
        self.assertEqual(requests, 1)
        self.assertEqual(hosts[0], "kjerringholmen.com")
        self.assertIn("3140", p.call_args[0][1]["textQuery"])
        self.assertIn("websiteUri", p.call_args[0][2]["X-Goog-FieldMask"])

    def test_provider_error_is_swallowed_but_counted(self):
        with env(GOOGLE_PLACES_API_KEY="k"), mock.patch.object(gs, "_post_json", side_effect=OSError("quota")):
            self.assertEqual(gs.google_candidate_hosts(PROFILE), ([], 1, "google_places"))


if __name__ == "__main__":
    unittest.main()
