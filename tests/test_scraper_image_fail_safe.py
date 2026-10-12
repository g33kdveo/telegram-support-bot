import os
import tempfile
import unittest
from unittest.mock import patch

from scraper import (
    IMAGE_ALL_FAIL_CHECK_COUNT,
    IMAGE_DOWNLOAD_BATCH_SIZE,
    IMAGE_DOWNLOAD_CONCURRENCY,
    RogersRoofingScraper,
    _apply_cached_image_references,
    _fetch_image_variants,
)


class ScraperImageSafetyTests(unittest.TestCase):
    def test_image_downloads_run_in_bounded_batches(self):
        self.assertGreater(IMAGE_DOWNLOAD_BATCH_SIZE, 0)
        self.assertLessEqual(IMAGE_DOWNLOAD_CONCURRENCY, 20)
        self.assertLessEqual(IMAGE_DOWNLOAD_BATCH_SIZE, 100)
        self.assertGreater(IMAGE_ALL_FAIL_CHECK_COUNT, IMAGE_DOWNLOAD_BATCH_SIZE)

    def test_concrete_size_format_generates_larger_variants_and_keeps_source_size(self):
        scraper = RogersRoofingScraper()
        variants = scraper._get_image_url_variants(
            "x375-015-1791589213355.png.webp",
            "/uploads/products/",
        )

        self.assertEqual(
            variants,
            [
                "https://img.chadsflooring.bz/products/x950-015-1791589213355.png.webp",
                "https://img.chadsflooring.bz/products/x750-015-1791589213355.png.webp",
                "https://img.chadsflooring.bz/products/x450-015-1791589213355.png.webp",
                "https://img.chadsflooring.bz/products/x400-015-1791589213355.png.webp",
                "https://img.chadsflooring.bz/products/x375-015-1791589213355.png.webp",
                "https://img.chadsflooring.bz/products/x325-015-1791589213355.png.webp",
                "https://img.chadsflooring.bz/products/x300-015-1791589213355.png.webp",
            ],
        )

    def test_products_path_uses_image_cdn(self):
        scraper = RogersRoofingScraper()

        self.assertEqual(
            scraper._resolve_image_url("/products/x250-AW4A5015.webp", "/products/"),
            "https://img.chadsflooring.bz/products/x250-AW4A5015.webp",
        )

    def test_bare_filename_uses_image_cdn_path(self):
        scraper = RogersRoofingScraper()

        self.assertEqual(
            scraper._resolve_image_url("x250-AW4A5015.webp", "/"),
            "https://img.chadsflooring.bz/products/x250-AW4A5015.webp",
        )

    def test_successful_downloads_are_used_by_group_and_variant_images(self):
        groups = [{
            "imgs": {"main": "group-image"},
            "products": [{"images": ["variant-image", {"unsupported": True}]}],
        }]

        _apply_cached_image_references(
            groups,
            {"group-image": "group.webp", "variant-image": "variant.webp"},
        )

        self.assertEqual(groups[0]["imgs"]["main"], "__cached__:group.webp")
        self.assertEqual(
            groups[0]["products"][0]["images"],
            ["__cached__:variant.webp", {"unsupported": True}],
        )


class ScraperImageBatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_continues_after_individual_failures(self):
        groups = [{"imgs": {"main": f"x750-image{index}.webp"}} for index in range(35)]
        fetched = []

        async def fake_fetch(_client, variants):
            image_url = variants[0]
            fetched.append(image_url)
            image_index = int(image_url.rsplit("image", 1)[1].split(".", 1)[0])
            if image_index < 3:
                return None, None, "404 image not found", False
            return b"image-bytes" * 60, image_url, None, False

        scraper = RogersRoofingScraper()

        with tempfile.TemporaryDirectory() as temp_dir:
            fake_file = os.path.join(temp_dir, "scraper.py")
            with patch("scraper.os.path.abspath", return_value=fake_file), patch(
                "scraper._fetch_image_variants", side_effect=fake_fetch
            ):
                await scraper._download_images(None, groups, "/uploads/products/")

        self.assertEqual(len(fetched), 35)
        self.assertEqual(groups[2]["imgs"]["main"], "x750-image2.webp")
        self.assertTrue(groups[3]["imgs"]["main"].startswith("__cached__:"))
        self.assertTrue(groups[34]["imgs"]["main"].startswith("__cached__:"))

    async def test_stops_early_when_initial_images_all_fail(self):
        groups = [{"imgs": {"main": f"x750-image{index}.webp"}} for index in range(200)]
        fetched = []

        async def fake_fetch(_client, variants):
            fetched.append(variants[0])
            return None, None, "404 image not found", False

        scraper = RogersRoofingScraper()

        with tempfile.TemporaryDirectory() as temp_dir:
            fake_file = os.path.join(temp_dir, "scraper.py")
            with patch("scraper.os.path.abspath", return_value=fake_file), patch(
                "scraper._fetch_image_variants", side_effect=fake_fetch
            ):
                await scraper._download_images(None, groups, "/uploads/products/")

        self.assertEqual(len(fetched), IMAGE_DOWNLOAD_BATCH_SIZE * 2)

    async def test_stops_after_image_host_returns_429(self):
        groups = [{"imgs": {"main": f"x750-image{index}.webp"}} for index in range(200)]
        fetched = []

        async def fake_fetch(_client, variants):
            fetched.append(variants[0])
            return None, variants[0], "429", True

        scraper = RogersRoofingScraper()

        with tempfile.TemporaryDirectory() as temp_dir:
            fake_file = os.path.join(temp_dir, "scraper.py")
            with patch("scraper.os.path.abspath", return_value=fake_file), patch(
                "scraper._fetch_image_variants", side_effect=fake_fetch
            ):
                await scraper._download_images(None, groups, "/uploads/products/")

        self.assertEqual(len(fetched), IMAGE_DOWNLOAD_BATCH_SIZE)

    async def test_fetch_image_variants_rejects_non_image_then_uses_image(self):
        class Response:
            def __init__(self, content_type, content):
                self.status_code = 200
                self.is_success = True
                self.headers = {"Content-Type": content_type}
                self.content = content

        class FakeClient:
            def __init__(self):
                self.responses = [Response("text/html", b"login page"), Response("image/webp", b"x" * 800)]
                self.requested = []

            async def get(self, image_url):
                self.requested.append(image_url)
                return self.responses.pop(0)

        urls = [
            "https://img.chadsflooring.bz/products/first.webp",
            "https://img.chadsflooring.bz/products/second.webp",
        ]
        client = FakeClient()
        result = await _fetch_image_variants(client, urls)

        self.assertEqual(result[0], b"x" * 800)
        self.assertEqual(result[1], urls[1])
        self.assertEqual(client.requested, urls)

    async def test_fetch_image_variants_uses_largest_available_size(self):
        class Response:
            def __init__(self, status_code, content_type, content):
                self.status_code = status_code
                self.is_success = status_code == 200
                self.headers = {"Content-Type": content_type}
                self.content = content

        responses = [
            Response(404, "text/html", b"not found"),
            Response(200, "image/webp", b"x" * 900),
        ]
        candidate_urls = [
            "https://img.chadsflooring.bz/products/x950-photo.webp",
            "https://img.chadsflooring.bz/products/x750-photo.webp",
            "https://img.chadsflooring.bz/products/x450-photo.webp",
        ]

        class FakeClient:
            def __init__(self):
                self.responses = list(responses)
                self.requested = []

            async def get(self, image_url):
                self.requested.append(image_url)
                return self.responses.pop(0)

        client = FakeClient()
        image_bytes, chosen_url, error, rate_limited = await _fetch_image_variants(
            client,
            candidate_urls,
        )

        self.assertEqual(image_bytes, b"x" * 900)
        self.assertEqual(chosen_url, candidate_urls[1])
        self.assertIsNone(error)
        self.assertFalse(rate_limited)
        self.assertEqual(client.requested, candidate_urls[:2])


if __name__ == "__main__":
    unittest.main()
