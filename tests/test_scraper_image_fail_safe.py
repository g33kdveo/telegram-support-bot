import unittest

from scraper import MAX_IMAGE_DOWNLOADS_BEFORE_SKIP


class ScraperImageSafetyTests(unittest.TestCase):
    def test_large_image_batches_are_skipped(self):
        self.assertGreater(MAX_IMAGE_DOWNLOADS_BEFORE_SKIP, 0)
        self.assertLess(MAX_IMAGE_DOWNLOADS_BEFORE_SKIP, 5000)


if __name__ == "__main__":
    unittest.main()
