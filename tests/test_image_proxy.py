import inspect
import unittest

import bot
from bot import _build_image_request_headers, _resolve_image_url


class ImageProxyTests(unittest.TestCase):
    def test_build_image_request_headers_includes_cookie_session(self):
        headers = _build_image_request_headers(
            "https://chadsflooring.bz/uploads/products/test.jpg",
            cookies=[{"name": "session", "value": "abc123"}, {"name": "other", "value": "x"}],
            user_agent="Mozilla/5.0 test-agent",
        )
        self.assertEqual(headers["User-Agent"], "Mozilla/5.0 test-agent")
        self.assertIn("session=abc123", headers["Cookie"])
        self.assertIn("other=x", headers["Cookie"])

    def test_resolve_image_url_from_relative_path(self):
        resolved = _resolve_image_url("/uploads/products/test.jpg", "https://chadsflooring.bz")
        self.assertEqual(resolved, "https://chadsflooring.bz/uploads/products/test.jpg")

    def test_resolve_image_url_keeps_current_size_format(self):
        resolved = _resolve_image_url("x750-something.jpg.webp", "https://chadsflooring.bz")
        self.assertEqual(resolved, "https://chadsflooring.bz/x750-something.jpg.webp")

    def test_monitor_jobs_do_not_reenter_the_same_lock(self):
        auto_refresh_source = inspect.getsource(bot.auto_refresh_job)
        stock_monitor_source = inspect.getsource(bot.stock_monitor_job)
        self.assertNotIn("SCRAPE_COORDINATOR_LOCK", auto_refresh_source)
        self.assertNotIn("SCRAPE_COORDINATOR_LOCK", stock_monitor_source)


if __name__ == "__main__":
    unittest.main()
