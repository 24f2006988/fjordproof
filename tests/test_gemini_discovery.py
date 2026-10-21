import io
import os
import unittest
import urllib.error
from unittest import mock

from norway_company_agent import gemini_discovery as gd

PROFILE = {"organisation_number": "922297738", "name": "NOBISCURA AS", "industry_label": "Barneverntjenester",
           "evidence": {"registry_live": {"value": {"business_address": {"adresse": ["Lienga 6"], "postnummer": "1414", "poststed": "TROLLÅSEN"}}}}}


def reply(text, queries=(), titles=()):
    return {"candidates": [{"content": {"parts": [{"text": text}]},
                            "groundingMetadata": {"webSearchQueries": list(queries), "groundingChunks": [{"web": {"title": t}} for t in titles]}}],
            "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 10}}


class GeminiDiscovery(unittest.TestCase):
    def setUp(self):
        gd._search_blocked = False

    def test_no_key_no_request(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": ""}), mock.patch.object(gd, "_post") as post:
            self.assertEqual(gd.candidate_hosts(PROFILE), ([], 0, 0.0, None))
            post.assert_not_called()

    def test_domains_parsed_directories_dropped_cost_declared(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "k"}), \
             mock.patch.object(gd, "_post", return_value=reply("The website is https://www.nobiscura.no/ (see also proff.no)", ["nobiscura"], ["nobiscura.no", "proff.no"])):
            hosts, reqs, cost, mode = gd.candidate_hosts(PROFILE)
        self.assertEqual((hosts, reqs, mode), (["nobiscura.no"], 1, "search"))
        self.assertAlmostEqual(cost, gd.SEARCH_QUERY_USD + (100 * 0.10 + 10 * 0.40) / 1e6)

    def test_no_search_quota_falls_back_to_model_only(self):
        quota = urllib.error.HTTPError("u", 429, "quota", {}, io.BytesIO(b""))
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "k"}), \
             mock.patch.object(gd, "_post", side_effect=[quota, reply("nobiscura.no")]) as post:
            hosts, reqs, _, mode = gd.candidate_hosts(PROFILE)
        self.assertEqual((hosts, reqs, mode), (["nobiscura.no"], 2, "model"))
        self.assertNotIn("tools", post.call_args_list[1].args[0])
        self.assertTrue(gd._search_blocked)

    def test_switch_off(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "k", "SIGNALPOST_GEMINI_DISCOVERY": "off"}), mock.patch.object(gd, "_post") as post:
            self.assertEqual(gd.candidate_hosts(PROFILE)[0], [])
            post.assert_not_called()
