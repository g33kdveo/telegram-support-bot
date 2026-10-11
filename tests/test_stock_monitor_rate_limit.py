import json
import unittest
from unittest.mock import AsyncMock, patch

import bot
from scraper import RogersRoofingScraper, fetch_authenticated_stock_snapshot


class StockMonitorRateLimitTests(unittest.TestCase):
    def test_repeated_rate_limits_back_off_and_reset_after_recovery(self):
        bot._reset_stock_api_rate_limit()
        try:
            self.assertEqual(bot._stock_api_rate_limit_delay(), 300)
            self.assertEqual(bot._stock_api_rate_limit_delay(), 600)
            self.assertEqual(bot._stock_api_rate_limit_delay(), 1200)
            self.assertEqual(bot._stock_api_rate_limit_delay(), 1800)
            self.assertEqual(bot._stock_api_rate_limit_delay(), 1800)
            self.assertEqual(bot._stock_api_rate_limit_delay(2400), 2400)

            bot._reset_stock_api_rate_limit()
            self.assertEqual(bot._stock_api_rate_limit_delay(), 300)
        finally:
            bot._reset_stock_api_rate_limit()

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

    def test_429_without_retry_after_uses_five_minute_fallback(self):
        class RateLimitedResponse:
            status_code = 429
            headers = {}

        with patch(
            "scraper.get_stored_cookies",
            return_value=([{"name": "session", "value": "test"}], "test-agent"),
        ), patch("scraper.requests.get", return_value=RateLimitedResponse()):
            result = fetch_authenticated_stock_snapshot("test-api-key")

        self.assertEqual(result["retry_after"], 300)


class FullScrapeRateLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_schedules_scrape_even_if_command_registration_fails(self):
        class FakeJobQueue:
            scheduled = None

            def run_once(self, callback, **kwargs):
                self.scheduled = (callback, kwargs)

        class FakeApplication:
            def __init__(self):
                self.job_queue = FakeJobQueue()

        app = FakeApplication()
        with patch.object(
            bot,
            "set_commands",
            new=AsyncMock(side_effect=ConnectionError("Telegram API unavailable")),
        ) as set_commands:
            await bot.startup_post_init(app)

        callback, job_kwargs = app.job_queue.scheduled
        self.assertEqual(callback, bot.auto_refresh_job)
        self.assertEqual(job_kwargs["when"], 0)
        self.assertEqual(job_kwargs["name"], "startup_product_refresh")
        set_commands.assert_awaited_once_with(app)

    async def test_browser_scrape_stops_after_one_429_and_keeps_retry_delay(self):
        class RateLimitedPage:
            calls = 0

            async def evaluate(self, *_args):
                self.calls += 1
                return json.dumps({"_error": "HTTP 429", "retry_after": 90})

        page = RateLimitedPage()
        scraper = RogersRoofingScraper(api_key="test-api-key")
        result = await scraper._fetch_scrape_endpoint(page)

        self.assertIsNone(result)
        self.assertEqual(page.calls, 1)
        self.assertEqual(scraper.rate_limit_retry_after, 90)

    def test_rate_limited_full_scrape_schedules_retry(self):
        class FakeJob:
            def __init__(self):
                self.removed = False

            def schedule_removal(self):
                self.removed = True

        class FakeJobQueue:
            def __init__(self):
                self.jobs = [FakeJob()]
                self.scheduled = None

            def get_jobs_by_name(self, _name):
                return self.jobs

            def run_once(self, callback, **kwargs):
                self.scheduled = (callback, kwargs)

        class FakeContext:
            job_queue = FakeJobQueue()

        context = FakeContext()
        bot._schedule_rate_limited_auto_refresh(context, 90)

        self.assertTrue(context.job_queue.jobs[0].removed)
        self.assertEqual(context.job_queue.scheduled[0], bot.auto_refresh_job)
        self.assertEqual(context.job_queue.scheduled[1]["when"], 90)


if __name__ == "__main__":
    unittest.main()