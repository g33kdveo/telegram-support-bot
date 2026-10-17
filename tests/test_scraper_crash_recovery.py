import unittest
from unittest.mock import Mock, patch

from pyppeteer.errors import NetworkError

from scraper import RogersRoofingScraper, _is_retriable_browser_error


class BrowserErrorDetectionTests(unittest.TestCase):
    def test_target_closed_is_retriable(self):
        self.assertTrue(_is_retriable_browser_error(NetworkError("Protocol error Runtime.callFunctionOn: Target closed.")))
        self.assertTrue(_is_retriable_browser_error(Exception("Connection closed unexpectedly")))

    def test_unrelated_errors_are_not_retriable(self):
        self.assertFalse(_is_retriable_browser_error(ValueError("bad value")))
        self.assertFalse(_is_retriable_browser_error(Exception("HTTP 401 Unauthorized")))


class BrowserCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_kills_unresponsive_process(self):
        scraper = RogersRoofingScraper()
        process = Mock()
        process.poll.return_value = None
        browser = Mock()
        browser.process = process

        async def close():
            pass

        browser.close = close
        await scraper._cleanup_browser(browser)

        process.kill.assert_called_once()

    async def test_cleanup_skips_exited_process_and_survives_close_failure(self):
        scraper = RogersRoofingScraper()
        process = Mock()
        process.poll.return_value = 0
        browser = Mock()
        browser.process = process

        async def close():
            raise RuntimeError("connection already closed")

        browser.close = close
        await scraper._cleanup_browser(browser)

        process.kill.assert_not_called()

    async def test_cleanup_tolerates_missing_process(self):
        scraper = RogersRoofingScraper()
        browser = Mock()
        browser.process = None

        async def close():
            pass

        browser.close = close
        await scraper._cleanup_browser(browser)


class BrowserRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_scrape_retries_once_after_browser_crash(self):
        scraper = RogersRoofingScraper()
        attempts = []

        async def fake_scrape_once(download_images=True):
            attempts.append(download_images)
            if len(attempts) == 1:
                scraper._last_browser_crash = True
                return {"data": []}
            scraper._last_browser_crash = False
            return {"data": [{"id": "g1"}]}

        async def instant_sleep(_seconds):
            return None

        with patch.object(scraper, "_scrape_once", side_effect=fake_scrape_once), patch(
            "scraper.asyncio.sleep", instant_sleep
        ):
            result = await scraper._scrape_async()

        self.assertEqual(result["data"], [{"id": "g1"}])
        self.assertEqual(len(attempts), 2)

    async def test_scrape_does_not_retry_rate_limit(self):
        scraper = RogersRoofingScraper()
        attempts = 0

        async def fake_scrape_once(download_images=True):
            nonlocal attempts
            attempts += 1
            return {"data": [], "rate_limited": True, "retry_after": 300}

        with patch.object(scraper, "_scrape_once", side_effect=fake_scrape_once):
            result = await scraper._scrape_async()

        self.assertTrue(result["rate_limited"])
        self.assertEqual(attempts, 1)

    async def test_scrape_stops_after_max_attempts(self):
        scraper = RogersRoofingScraper()
        attempts = 0

        async def fake_scrape_once(download_images=True):
            nonlocal attempts
            attempts += 1
            scraper._last_browser_crash = True
            return {"data": []}

        async def instant_sleep(_seconds):
            return None

        with patch.object(scraper, "_scrape_once", side_effect=fake_scrape_once), patch(
            "scraper.asyncio.sleep", instant_sleep
        ):
            result = await scraper._scrape_async()

        self.assertEqual(result["data"], [])
        self.assertEqual(attempts, 2)


if __name__ == "__main__":
    unittest.main()
