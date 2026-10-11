import base64
import os
import tempfile
import unittest
from unittest.mock import patch

from scraper import (
    IMAGE_DOWNLOAD_BATCH_SIZE,
    RogersRoofingScraper,
    _apply_cached_image_references,
)


class ScraperImageSafetyTests(unittest.TestCase):
    def test_image_downloads_run_in_bounded_batches(self):
        self.assertGreater(IMAGE_DOWNLOAD_BATCH_SIZE, 0)
        self.assertLessEqual(IMAGE_DOWNLOAD_BATCH_SIZE, 50)

    def test_concrete_size_format_is_not_retried_as_duplicate_variants(self):
        scraper = RogersRoofingScraper()
        variants = scraper._get_image_url_variants(
            "x750-product.jpg.webp",
            "/uploads/products/",
        )

        self.assertEqual(variants, ["https://chadsflooring.bz/uploads/products/x750-product.jpg.webp"])

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
    async def test_download_continues_after_many_individual_failures(self):
        class FakePage:
            calls = 0

            async def evaluate(self, _script, variants_map):
                self.calls += 1
                results = {}
                for image_template in variants_map:
                    image_index = int(image_template.rsplit("image", 1)[1].split(".", 1)[0])
                    if image_index < 200:
                        results[image_template] = {"error": "not found"}
                    else:
                        image_data = base64.b64encode(b"image-bytes" * 60).decode()
                        results[image_template] = {"data": f"data:image/webp;base64,{image_data}"}
                return results

        groups = [{"imgs": {"main": f"x_imgvariantsize-image{index}.webp"}} for index in range(205)]
        page = FakePage()
        scraper = RogersRoofingScraper()

        with tempfile.TemporaryDirectory() as temp_dir:
            fake_file = os.path.join(temp_dir, "scraper.py")
            with patch("scraper.os.path.abspath", return_value=fake_file):
                await scraper._download_images(page, groups, "/uploads/products/")

        self.assertEqual(page.calls, 14)
        self.assertEqual(groups[199]["imgs"]["main"], "x_imgvariantsize-image199.webp")
        self.assertTrue(groups[200]["imgs"]["main"].startswith("__cached__:"))
        self.assertTrue(groups[204]["imgs"]["main"].startswith("__cached__:"))


if __name__ == "__main__":
    unittest.main()
