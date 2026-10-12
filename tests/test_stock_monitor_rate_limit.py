import asyncio
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import AsyncMock, patch

import bot
from scraper import RogersRoofingScraper, fetch_authenticated_stock_snapshot


class StockMonitorRateLimitTests(unittest.TestCase):
    def test_stock_snapshot_merges_same_named_group_rows_and_keeps_all_variants(self):
        payload = {
            "data": [
                {"id": "group-1", "name": "Same group", "products": [{"id": "v1", "name": "First", "qty": 2}]},
                {"id": "group-1", "name": "Same group", "products": [{"id": "v2", "name": "Second", "qty": 3}]},
            ]
        }

        class Response:
            status_code = 200
            url = "https://chadsflooring.bz/api/products/scrape"
            headers = {"Content-Type": "application/json"}

            def raise_for_status(self):
                return None

            def json(self):
                return payload

        with patch(
            "scraper.get_stored_cookies",
            return_value=([{"name": "session", "value": "test"}], "test-agent"),
        ), patch("scraper.requests.get", return_value=Response()):
            result = fetch_authenticated_stock_snapshot("test-api-key")

        self.assertEqual(result["source_group_rows"], 2)
        self.assertEqual(len(result["snapshot"]), 1)
        self.assertEqual(len(result["snapshot"]["group-1"]["variants"]), 2)
        self.assertEqual(result["variant_count"], 2)

    def test_stock_snapshot_preserves_duplicate_group_and_variant_ids(self):
        payload = {
            "data": [
                {
                    "id": "group-1",
                    "name": "Group one",
                    "products": [{"id": "variant-1", "name": "First variant", "qty": 2}],
                },
                {
                    "id": "group-1",
                    "name": "Group duplicate",
                    "products": [
                        {"id": "variant-1", "name": "Second variant", "qty": 3},
                        {"id": "variant-1", "name": "Third variant", "qty": 4},
                    ],
                },
            ]
        }

        class Response:
            status_code = 200
            url = "https://chadsflooring.bz/api/products/scrape"
            headers = {"Content-Type": "application/json"}

            def raise_for_status(self):
                return None

            def json(self):
                return payload

        with patch(
            "scraper.get_stored_cookies",
            return_value=([{"name": "session", "value": "test"}], "test-agent"),
        ), patch("scraper.requests.get", return_value=Response()):
            result = fetch_authenticated_stock_snapshot("test-api-key")

        self.assertEqual(len(result["snapshot"]), 2)
        self.assertEqual(len(result["snapshot"]["group-1"]["variants"]), 1)
        duplicate_variants = result["snapshot"]["group-1::duplicate:2"]["variants"]
        self.assertEqual(len(duplicate_variants), 2)

    def test_stock_snapshot_merges_same_named_duplicate_group_rows(self):
        payload = {
            "data": [
                {"id": "group-1", "name": "Same group", "products": [{"id": "v1", "name": "First", "qty": 2}]},
                {"id": "group-1", "name": "Same group", "products": [{"id": "v2", "name": "Second", "qty": 3}]},
            ]
        }

        class Response:
            status_code = 200
            url = "https://chadsflooring.bz/api/products/scrape"
            headers = {"Content-Type": "application/json"}

            def raise_for_status(self):
                return None

            def json(self):
                return payload

        with patch(
            "scraper.get_stored_cookies",
            return_value=([{"name": "session", "value": "test"}], "test-agent"),
        ), patch("scraper.requests.get", return_value=Response()):
            result = fetch_authenticated_stock_snapshot("test-api-key")

        self.assertEqual(result["source_group_rows"], 2)
        self.assertEqual(len(result["snapshot"]), 1)
        self.assertEqual(set(result["snapshot"]["group-1"]["variants"]), {"v1", "v2"})

    def test_full_and_checkstock_snapshots_preserve_duplicate_group_ids(self):
        scrape_result = {
            "data": [
                {"id": "group-1", "name": "First", "products": [{"id": "v1", "name": "V1", "qty": 1}]},
                {"id": "group-1", "name": "Second", "products": [{"id": "v2", "name": "V2", "qty": 2}]},
            ]
        }

        full_snapshot = bot._product_snapshot(scrape_result)
        checkstock_snapshot = bot._stock_snapshot(scrape_result)

        self.assertEqual(set(full_snapshot), {"group-1", "group-1::duplicate:2"})
        self.assertEqual(len(checkstock_snapshot), 2)
        self.assertEqual({item["name"] for item in checkstock_snapshot.values()}, {"V1", "V2"})

    def test_full_snapshot_merges_same_named_duplicate_groups(self):
        scrape_result = {
            "data": [
                {"id": "group-1", "name": "Same group", "products": [{"id": "v1", "name": "First", "qty": 1}]},
                {"id": "group-1", "name": "Same group", "products": [{"id": "v2", "name": "Second", "qty": 2}]},
            ]
        }

        snapshot = bot._product_snapshot(scrape_result)
        stock_snapshot = bot._stock_snapshot(scrape_result)

        self.assertEqual(len(snapshot), 1)
        self.assertEqual(set(snapshot["group-1"]["variants"]), {"v1", "v2"})
        self.assertEqual(set(stock_snapshot), {"v1", "v2"})

    def test_full_snapshot_merges_same_named_group_rows(self):
        scrape_result = {
            "data": [
                {"id": "group-1", "name": "Same group", "products": [{"id": "v1", "name": "First", "qty": 1}]},
                {"id": "group-1", "name": "Same group", "products": [{"id": "v2", "name": "Second", "qty": 2}]},
            ]
        }

        snapshot = bot._product_snapshot(scrape_result)

        self.assertEqual(len(snapshot), 1)
        self.assertEqual(set(snapshot["group-1"]["variants"]), {"v1", "v2"})

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
    async def test_duplicate_variant_names_generate_one_new_variation_alert(self):
        previous_state = bot.SHOP_UPDATE_STATE
        previous_times = bot.SHOP_NOTIFICATION_TIMES
        baseline = {
            "data": [{"id": "g1", "name": "Flower", "products": [{"id": "old", "name": "Original", "qty": 5}]}]
        }
        current = {
            "data": [{
                "id": "g1",
                "name": "Flower",
                "products": [
                    {"id": "old", "name": "Original", "qty": 5},
                    {"id": "new-a", "name": "Blue Dream", "qty": 4},
                    {"id": "new-b", "name": "Blue  Dream", "qty": 4},
                ],
            }]
        }
        bot.SHOP_UPDATE_STATE = bot._normalize_product_snapshot(bot._product_snapshot(baseline))
        bot.SHOP_NOTIFICATION_TIMES = {}
        mock_bot = AsyncMock()
        try:
            with patch.object(bot, "_save_shop_update_state"):
                result = await bot.notify_shop_updates(mock_bot, current)
        finally:
            bot.SHOP_UPDATE_STATE = previous_state
            bot.SHOP_NOTIFICATION_TIMES = previous_times

        self.assertEqual(result["detected_changes"], 1)
        self.assertEqual(result["notifications"], 1)
        self.assertEqual(mock_bot.send_message.await_count, 1)
        alert_text = mock_bot.send_message.await_args.kwargs["text"]
        self.assertIn("New variation added", alert_text)
        self.assertIn("Blue Dream", alert_text)
        self.assertNotIn("New product added", alert_text)

    async def test_brand_new_group_gets_one_product_alert_with_variation_list(self):
        previous_state = bot.SHOP_UPDATE_STATE
        previous_times = bot.SHOP_NOTIFICATION_TIMES
        bot.SHOP_UPDATE_STATE = {}
        bot.SHOP_NOTIFICATION_TIMES = {}
        current = {
            "data": [{
                "id": "new-group",
                "name": "New Flower",
                "products": [
                    {"id": "v1", "name": "Sour Diesel", "qty": 5},
                    {"id": "v2", "name": "Gelato", "qty": 3},
                ],
            }]
        }
        mock_bot = AsyncMock()
        try:
            with patch.object(bot, "_save_shop_update_state"):
                result = await bot.notify_shop_updates(mock_bot, current)
        finally:
            bot.SHOP_UPDATE_STATE = previous_state
            bot.SHOP_NOTIFICATION_TIMES = previous_times

        self.assertEqual(result["detected_changes"], 1)
        self.assertEqual(mock_bot.send_message.await_count, 1)
        alert_text = mock_bot.send_message.await_args.kwargs["text"]
        self.assertIn("New product added", alert_text)
        self.assertIn("Sour Diesel", alert_text)
        self.assertIn("Gelato", alert_text)
        self.assertNotIn("New variation added", alert_text)

    async def test_cooldown_suppressed_stock_change_is_not_reported_as_zero_changes(self):
        previous_state = bot.SHOP_UPDATE_STATE
        previous_times = bot.SHOP_NOTIFICATION_TIMES
        previous_comparisons = bot.STOCK_MONITOR_COMPARISONS_SINCE_FULL
        previous_changes = bot.STOCK_CHANGES_DETECTED_SINCE_FULL
        baseline_data = {
            "data": [{"id": "group-1", "name": "Item", "products": [{"id": "v1", "name": "Variant", "qty": 5}]}]
        }
        updated_data = {
            "data": [{"id": "group-1", "name": "Item", "products": [{"id": "v1", "name": "Variant", "qty": 6}]}]
        }
        bot.SHOP_UPDATE_STATE = bot._normalize_product_snapshot(bot._product_snapshot(baseline_data))
        bot.SHOP_NOTIFICATION_TIMES = {"group:group-1": bot.time.time()}
        bot.STOCK_MONITOR_COMPARISONS_SINCE_FULL = 0
        bot.STOCK_CHANGES_DETECTED_SINCE_FULL = 0
        try:
            with patch.object(bot, "_save_shop_update_state"):
                comparison = await bot.notify_shop_updates(
                    object(),
                    updated_data,
                    log_context="[source=stock-monitor run=test-run]",
                )
            bot.STOCK_MONITOR_COMPARISONS_SINCE_FULL += 1
            bot.STOCK_CHANGES_DETECTED_SINCE_FULL += comparison["detected_changes"]
            mock_bot = AsyncMock()
            await bot._report_stock_cycle_summary(
                type("Context", (), {"bot": mock_bot})(),
                "full-run",
            )
            mock_bot.send_message.assert_not_awaited()
        finally:
            bot.SHOP_UPDATE_STATE = previous_state
            bot.SHOP_NOTIFICATION_TIMES = previous_times
            bot.STOCK_MONITOR_COMPARISONS_SINCE_FULL = previous_comparisons
            bot.STOCK_CHANGES_DETECTED_SINCE_FULL = previous_changes

    async def test_zero_change_summary_sends_once_for_full_cycle(self):
        previous_comparisons = bot.STOCK_MONITOR_COMPARISONS_SINCE_FULL
        previous_changes = bot.STOCK_CHANGES_DETECTED_SINCE_FULL
        bot.STOCK_MONITOR_COMPARISONS_SINCE_FULL = 2
        bot.STOCK_CHANGES_DETECTED_SINCE_FULL = 0
        mock_bot = AsyncMock()
        context = type("Context", (), {"bot": mock_bot})()
        try:
            await bot._report_stock_cycle_summary(context, "full-run")
            mock_bot.send_message.assert_awaited_once()
            self.assertIn("No stock changes", mock_bot.send_message.await_args.kwargs["text"])
        finally:
            bot.STOCK_MONITOR_COMPARISONS_SINCE_FULL = previous_comparisons
            bot.STOCK_CHANGES_DETECTED_SINCE_FULL = previous_changes

    async def test_successful_login_notification_is_sent_once_per_run(self):
        previous_sent = bot.LOGIN_NOTIFICATION_SENT
        previous_runs = bot.LOGIN_NOTIFIED_RUNS.copy()
        bot.LOGIN_NOTIFICATION_SENT = False
        bot.LOGIN_NOTIFIED_RUNS.clear()
        mock_bot = AsyncMock()
        try:
            await bot.notify_login_success(mock_bot, "run-one")
            await bot.notify_login_success(mock_bot, "run-one")
            await bot.notify_login_success(mock_bot, "run-two")
            self.assertEqual(mock_bot.send_message.await_count, 2)
        finally:
            bot.LOGIN_NOTIFICATION_SENT = previous_sent
            bot.LOGIN_NOTIFIED_RUNS.clear()
            bot.LOGIN_NOTIFIED_RUNS.update(previous_runs)

    async def test_monitor_logs_when_no_stock_changes_are_detected(self):
        previous_state = bot.SHOP_UPDATE_STATE
        previous_times = bot.SHOP_NOTIFICATION_TIMES
        current_data = {
            "data": [{
                "id": "item-1",
                "name": "Item",
                "products": [{"id": "v1", "name": "Variant", "qty": 5}],
            }]
        }
        baseline = bot._normalize_product_snapshot(bot._product_snapshot(current_data))
        bot.SHOP_UPDATE_STATE = baseline
        bot.SHOP_NOTIFICATION_TIMES = {}
        output = io.StringIO()
        try:
            with redirect_stdout(output):
                with patch.object(bot, "_save_shop_update_state"):
                    await bot.notify_shop_updates(
                        object(),
                        current_data,
                        log_context="[source=stock-monitor run=test-run]",
                    )
        finally:
            bot.SHOP_UPDATE_STATE = previous_state
            bot.SHOP_NOTIFICATION_TIMES = previous_times

        self.assertIn(
            "comparison complete: 1 groups, 0 changes detected, 0 notifications eligible",
            output.getvalue(),
        )

    async def test_quantity_decrease_counts_as_real_stock_change(self):
        previous_state = bot.SHOP_UPDATE_STATE
        previous_times = bot.SHOP_NOTIFICATION_TIMES
        data_before = {"data": [{"id": "g1", "name": "Item", "products": [{"id": "v1", "name": "Variant", "qty": 8}]}]}
        data_after = {"data": [{"id": "g1", "name": "Item", "products": [{"id": "v1", "name": "Variant", "qty": 4}]}]}
        bot.SHOP_UPDATE_STATE = bot._normalize_product_snapshot(bot._product_snapshot(data_before))
        bot.SHOP_NOTIFICATION_TIMES = {}
        mock_bot = AsyncMock()
        try:
            with patch.object(bot, "_save_shop_update_state"):
                result = await bot.notify_shop_updates(mock_bot, data_after)
        finally:
            bot.SHOP_UPDATE_STATE = previous_state
            bot.SHOP_NOTIFICATION_TIMES = previous_times

        self.assertEqual(result["detected_changes"], 1)
        self.assertEqual(result["notifications"], 1)
        self.assertIn("8 → 4", mock_bot.send_message.await_args.kwargs["text"])

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
    def test_monitor_cooldown_schedules_monitor_resume_not_full_scrape(self):
        class FakeJobQueue:
            def __init__(self):
                self.jobs = []

            def get_jobs_by_name(self, _name):
                return []

            def run_once(self, callback, **kwargs):
                self.jobs.append((callback, kwargs))

        class FakeContext:
            job_queue = FakeJobQueue()

        context = FakeContext()
        bot._schedule_stock_monitor_resume(context, 300)

        callback, job_kwargs = context.job_queue.jobs[0]
        self.assertEqual(callback, bot.resume_stock_monitor_job)
        self.assertNotEqual(callback, bot.auto_refresh_job)
        self.assertEqual(job_kwargs["when"], 300)
        self.assertEqual(job_kwargs["name"], "stock_monitor_rate_limit_resume")

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
            def __init__(self):
                self.jobs = []

            def run_once(self, callback, **kwargs):
                self.jobs.append((callback, kwargs))

        class FakeApplication:
            def __init__(self):
                self.job_queue = FakeJobQueue()

        app = FakeApplication()
        was_scheduled = bot.STARTUP_FULL_JOB_SCHEDULED
        bot.STARTUP_FULL_JOB_SCHEDULED = False
        with patch.object(
            bot,
            "set_commands",
            new=AsyncMock(side_effect=ConnectionError("Telegram API unavailable")),
        ) as set_commands:
            try:
                await bot.startup_post_init(app)
                await bot.startup_post_init(app)
            finally:
                bot.STARTUP_FULL_JOB_SCHEDULED = was_scheduled

        self.assertEqual(len(app.job_queue.jobs), 1)
        callback, job_kwargs = app.job_queue.jobs[0]
        self.assertEqual(callback, bot.auto_refresh_job)
        self.assertEqual(job_kwargs["when"], 0)
        self.assertEqual(job_kwargs["name"], "startup_product_refresh")
        self.assertEqual(job_kwargs["job_kwargs"]["misfire_grace_time"], 60)
        self.assertEqual(set_commands.await_count, 2)

    def test_scraper_logs_include_instance_source_and_run_ids(self):
        scraper = RogersRoofingScraper(
            run_id="run123",
            run_label="full",
            instance_id="inst456",
        )
        output = io.StringIO()
        with redirect_stdout(output):
            scraper._log("test event")

        self.assertIn("instance=inst456", output.getvalue())
        self.assertIn("source=full", output.getvalue())
        self.assertIn("run=run123", output.getvalue())

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

    async def test_full_scrape_failure_keeps_next_run_on_thirty_minute_schedule(self):
        class FakeJobQueue:
            def __init__(self):
                self.jobs = []

            def get_jobs_by_name(self, name):
                return [job for job in self.jobs if job[1].get("name") == name]

            def run_once(self, callback, **kwargs):
                self.jobs.append((callback, kwargs))

        class FakeContext:
            def __init__(self):
                self.job_queue = FakeJobQueue()

        context = FakeContext()
        previous_monitor_started = bot.STOCK_MONITOR_STARTED
        previous_cooldown = bot.STOCK_MONITOR_RATE_LIMIT_UNTIL
        bot.STOCK_MONITOR_STARTED = False
        bot.STOCK_MONITOR_RATE_LIMIT_UNTIL = bot.time.time() + 90
        try:
            with patch.object(bot, "_run_auto_refresh_job", new=AsyncMock(return_value=False)):
                await bot.auto_refresh_job(context)
        finally:
            bot.STOCK_MONITOR_STARTED = previous_monitor_started
            bot.STOCK_MONITOR_RATE_LIMIT_UNTIL = previous_cooldown

        self.assertEqual(len(context.job_queue.jobs), 1)
        callback, job_kwargs = context.job_queue.jobs[0]
        self.assertEqual(callback, bot.auto_refresh_job)
        self.assertEqual(job_kwargs["when"], 1800)
        self.assertEqual(job_kwargs["name"], "periodic_full_scrape")


if __name__ == "__main__":
    unittest.main()