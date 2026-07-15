import unittest

from retry_delay import format_retry_delay


class RetryDelayTests(unittest.TestCase):
    def test_seconds_and_singular_label(self) -> None:
        self.assertEqual(format_retry_delay(1), "1 second")
        self.assertEqual(format_retry_delay(45), "45 seconds")

    def test_minutes(self) -> None:
        self.assertEqual(format_retry_delay(60), "1 minute")
        self.assertEqual(format_retry_delay(180), "3 minutes")

    def test_hours(self) -> None:
        self.assertEqual(format_retry_delay(3_600), "1 hour")
        self.assertEqual(format_retry_delay(7_200), "2 hours")


if __name__ == "__main__":
    unittest.main()
