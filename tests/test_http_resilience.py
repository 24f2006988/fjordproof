import http.client
import unittest
from unittest import mock

from norway_company_agent import http as http_mod


class FetchJsonResilience(unittest.TestCase):
    def test_dropped_connection_is_retried_then_reported_not_raised(self):
        calls = []

        def boom(*a, **k):
            calls.append(1)
            raise http.client.RemoteDisconnected("Remote end closed connection without response")

        with mock.patch.object(http_mod.urllib.request, "urlopen", boom), mock.patch.object(http_mod.time, "sleep", lambda s: None):
            result = http_mod.fetch_json("https://data.brreg.no/x", attempts=3)
        self.assertEqual(len(calls), 3)
        self.assertEqual(result.status, 0)
        self.assertEqual(result.error, "RemoteDisconnected")

    def test_truncated_response_is_reported(self):
        def boom(*a, **k):
            raise http.client.IncompleteRead(b"{")

        with mock.patch.object(http_mod.urllib.request, "urlopen", boom), mock.patch.object(http_mod.time, "sleep", lambda s: None):
            result = http_mod.fetch_json("https://data.brreg.no/x", attempts=1)
        self.assertEqual(result.error, "IncompleteRead")


if __name__ == "__main__":
    unittest.main()
