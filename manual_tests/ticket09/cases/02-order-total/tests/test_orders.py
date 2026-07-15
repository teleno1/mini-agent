import unittest

from orders import order_total


class OrderTotalTests(unittest.TestCase):
    def test_gold_customer_receives_fifteen_percent_discount(self) -> None:
        self.assertEqual(order_total(10_000, "gold", 150), 8_500)

    def test_insufficient_points_receive_no_discount(self) -> None:
        self.assertEqual(order_total(10_000, "gold", 99), 10_000)

    def test_total_never_becomes_negative(self) -> None:
        self.assertEqual(order_total(-200, "silver", 200), 0)


if __name__ == "__main__":
    unittest.main()
