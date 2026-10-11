import unittest
from unittest.mock import patch

from scraper import fetch_authenticated_stock_snapshot


class StockMonitorRateLimitTests(unittest.TestCase):
    def test_429_returns_rate_limit_and_retry_after(self):
        class RateLimitedResponse:
            status_code = 429
            headers = {"Retry-After": "120"}

        with patch(
            "scraper.get_stored_cookies",
            return_value=([{"name": "session", "value": "test"}], "test-agent"),
        ), patch("scraper.requests.get", return_value=RateLimitedResponse()):
            result = fetch_authenticated_stock_snapshot("test-api-key")

        self.assertFalse(result["ok"])
        self.assertTrue(result["rate_limited"])
        self.assertEqual(result["retry_after"], 120)


if __name__ == "__main__":
    unittest.main()