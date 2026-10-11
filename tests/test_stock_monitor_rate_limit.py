import asyncio
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


class StockMonitorCooldownTests(unittest.IsolatedAsyncioTestCase):
    async def test_cooldown_tick_is_paused_not_counted_as_failure(self):
        previous_until = bot.STOCK_MONITOR_RATE_LIMIT_UNTIL
        previous_failures = bot.STOCK_MONITOR_FAILURE_COUNT
        bot.STOCK_MONITOR_RATE_LIMIT_UNTIL = bot.time.time() + 30
        bot.STOCK_MONITOR_FAILURE_COUNT = 4
        try:
            with patch.object(bot, "_claim_full_scrape", new=AsyncMock()):
                await bot._run_stock_monitor_job(object())
            self.assertEqual(bot.STOCK_MONITOR_FAILURE_COUNT, 4)
        finally:
            bot.STOCK_MONITOR_RATE_LIMIT_UNTIL = previous_until
            bot.STOCK_MONITOR_FAILURE_COUNT = previous_failures


class ScrapeCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_waiting_full_scrape_precedes_later_monitor(self):
        order = []
        monitor_started = asyncio.Event()
        release_monitor = asyncio.Event()
        full_started = asyncio.Event()
        release_full = asyncio.Event()

        async def run_monitor():
            async with bot._scrape_slot("monitor"):
                order.append("monitor-1")
                monitor_started.set()
                await release_monitor.wait()

        async def run_full():
            async with bot._scrape_slot("full"):
                order.append("full")
                full_started.set()
                await release_full.wait()

        async def run_later_monitor():
            async with bot._scrape_slot("monitor"):
                order.append("monitor-2")

        first_monitor = asyncio.create_task(run_monitor())
        await monitor_started.wait()
        full_scrape = asyncio.create_task(run_full())
        await asyncio.sleep(0)
        later_monitor = asyncio.create_task(run_later_monitor())
        await asyncio.sleep(0)
        release_monitor.set()
        await full_started.wait()

        self.assertEqual(order, ["monitor-1", "full"])
        release_full.set()
        await asyncio.gather(first_monitor, full_scrape, later_monitor)
        self.assertEqual(order, ["monitor-1", "full", "monitor-2"])

    def test_next_full_scrape_is_scheduled_after_completion_delay(self):
        class FakeJobQueue:
            def __init__(self):
                self.jobs = []
                self.scheduled = None

            def get_jobs_by_name(self, _name):
                return self.jobs

            def run_once(self, callback, **kwargs):
                self.scheduled = (callback, kwargs)

        class FakeContext:
            job_queue = FakeJobQueue()

        context = FakeContext()
        bot._schedule_next_full_scrape(context, 1800)

        self.assertEqual(context.job_queue.scheduled[0], bot.auto_refresh_job)
        self.assertEqual(context.job_queue.scheduled[1]["when"], 1800)
        self.assertEqual(context.job_queue.scheduled[1]["name"], "periodic_full_scrape")


class NotificationBaselineTests(unittest.IsolatedAsyncioTestCase):
    async def test_silent_full_refresh_does_not_overwrite_existing_baseline(self):
        previous_state = bot.SHOP_UPDATE_STATE
        previous_times = bot.SHOP_NOTIFICATION_TIMES
        baseline = {"old": {"name": "Old item", "category": "Flower", "variants": {}}}
        bot.SHOP_UPDATE_STATE = baseline.copy()
        bot.SHOP_NOTIFICATION_TIMES = {}
        try:
            await bot.notify_shop_updates(
                object(),
                {"data": [{"id": "new", "name": "New item", "products": []}]},
                send_notifications=False,
                update_baseline=False,
            )
            self.assertEqual(bot.SHOP_UPDATE_STATE, baseline)
        finally:
            bot.SHOP_UPDATE_STATE = previous_state
            bot.SHOP_NOTIFICATION_TIMES = previous_times


class FullScrapeRateLimitTests(unittest.IsolatedAsyncioTestCase):
    def test_stock_monitor_starts_once_after_full_scrape_with_sixty_second_delay(self):
        class FakeJobQueue:
            def __init__(self):
                self.jobs = []

            def run_repeating(self, callback, **kwargs):
                self.jobs.append((callback, kwargs))

        class FakeContext:
            def __init__(self):
                self.job_queue = FakeJobQueue()

        context = FakeContext()
        previous_started = bot.STOCK_MONITOR_STARTED
        bot.STOCK_MONITOR_STARTED = False
        try:
            bot._start_stock_monitor_after_full_scrape(context)
            bot._start_stock_monitor_after_full_scrape(context)
        finally:
            bot.STOCK_MONITOR_STARTED = previous_started

        self.assertEqual(len(context.job_queue.jobs), 1)
        callback, job_kwargs = context.job_queue.jobs[0]
        self.assertEqual(callback, bot.stock_monitor_job)
        self.assertEqual(job_kwargs["interval"], 60)
        self.assertEqual(job_kwargs["first"], 60)
        self.assertEqual(job_kwargs["name"], "stock_monitor")

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
        self.assertEqual(job_kwargs["job_kwargs"]["misfire_grace_time"], 60)
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