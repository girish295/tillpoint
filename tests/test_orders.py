import unittest
from datetime import datetime, timezone

from tillpoint import db, reports
from tillpoint.api import App
from tillpoint.catalog import OutOfStock


class OrderDeskTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect()
        self.app = App(self.conn)
        self.app.catalog.add_product("MUG", "Mug", 1200, stock=10)
        self.app.catalog.add_product("TEE", "T-shirt", 2500, stock=3)
        self.conn.execute("INSERT INTO coupons (code, kind, value) VALUES ('TEN', 'percent', 10)")

    def test_order_totals_with_tax(self):
        order = self.app.orders.create("ana", [("MUG", 2), ("TEE", 1)], region="CA")
        self.assertEqual(order["subtotal_cents"], 4900)
        self.assertEqual(order["tax_cents"], 355)
        self.assertEqual(order["total_cents"], 5255)
        self.assertEqual(self.app.catalog.get_product("MUG").stock, 8)

    def test_coupon_takes_ten_percent(self):
        order = self.app.orders.create("ana", [("MUG", 1)], coupon_code="TEN")
        self.assertEqual(order["discount_cents"], 120)
        self.assertEqual(order["total_cents"], 1080)

    def test_coupon_with_tax_calculates_tax_after_discount(self):
        # 1 MUG @ 1200, coupon TEN (10% = 120), CA tax (725 bps = 7.25%)
        # Taxable amount must be 1080 cents. Tax = round_half_up(1080 * 725, 10000) = 78 cents.
        order = self.app.orders.create("charlie", [("MUG", 1)], region="CA", coupon_code="TEN")
        self.assertEqual(order["subtotal_cents"], 1200)
        self.assertEqual(order["discount_cents"], 120)
        self.assertEqual(order["tax_cents"], 78)
        self.assertEqual(order["total_cents"], 1158)

    def test_out_of_stock(self):
        with self.assertRaises(OutOfStock):
            self.app.orders.create("ana", [("TEE", 4)])
        self.assertEqual(self.app.catalog.get_product("TEE").stock, 3)

    def test_payment_marks_order_paid(self):
        order = self.app.orders.create("ana", [("MUG", 1)])
        status, payload = self.app.handle("POST", "/webhooks/payments", {
            "id": "evt_1", "type": "payment.succeeded", "order_id": order["id"], "amount_cents": 1200})
        self.assertEqual((status, payload["status"]), (200, "paid"))

    def test_duplicate_payment_webhook_is_idempotent(self):
        order = self.app.orders.create("ana", [("MUG", 1)])
        status1, payload1 = self.app.handle("POST", "/webhooks/payments", {
            "id": "evt_dup_1", "type": "payment.succeeded", "order_id": order["id"], "amount_cents": 1200})
        self.assertEqual((status1, payload1["status"]), (200, "paid"))
        self.assertEqual(self.app.payments.paid_cents(order["id"]), 1200)

        # Webhook retry with the exact same event id
        status2, payload2 = self.app.handle("POST", "/webhooks/payments", {
            "id": "evt_dup_1", "type": "payment.succeeded", "order_id": order["id"], "amount_cents": 1200})
        self.assertEqual((status2, payload2["status"]), (200, "paid"))
        self.assertEqual(self.app.payments.paid_cents(order["id"]), 1200)

        rows = self.conn.execute("SELECT * FROM payments WHERE event_id = 'evt_dup_1'").fetchall()
        self.assertEqual(len(rows), 1)

    def test_cancel_paid_order_restocks(self):
        order = self.app.orders.create("ana", [("TEE", 2)])
        self.app.payments.handle_event(
            {"id": "evt_2", "type": "payment.succeeded", "order_id": order["id"], "amount_cents": 5000})
        self.app.orders.cancel(order["id"])
        self.assertEqual(self.app.catalog.get_product("TEE").stock, 3)

    def test_cancel_pending_order_restocks(self):
        order = self.app.orders.create("bob", [("TEE", 2)])
        self.assertEqual(order["status"], "pending")
        self.assertEqual(self.app.catalog.get_product("TEE").stock, 1)

        self.app.orders.cancel(order["id"])
        self.assertEqual(self.app.catalog.get_product("TEE").stock, 3)

        # Repeating cancel must not restock again
        self.app.orders.cancel(order["id"])
        self.assertEqual(self.app.catalog.get_product("TEE").stock, 3)

    def test_daily_sales(self):
        order = self.app.orders.create("ana", [("MUG", 1)])
        noon = datetime(2025, 3, 10, 12, 0, tzinfo=timezone.utc)
        self.app.payments.handle_event(
            {"id": "evt_3", "type": "payment.succeeded", "order_id": order["id"], "amount_cents": 1200}, now=noon)
        self.assertEqual(reports.daily_sales(self.conn, "2025-03-10"),
                         {"day": "2025-03-10", "revenue_cents": 1200, "orders": 1})

    def test_india_morning_sales_in_correct_day_report(self):
        # India is UTC+5:30 (+330 minutes)
        # 7:30 AM on March 10, 2025 in India is 02:00 AM UTC on March 10, 2025
        order = self.app.orders.create("deepa", [("MUG", 1)])
        india_morning_utc = datetime(2025, 3, 10, 2, 0, tzinfo=timezone.utc)
        self.app.payments.handle_event(
            {"id": "evt_india_1", "type": "payment.succeeded", "order_id": order["id"], "amount_cents": 1200},
            now=india_morning_utc,
        )

        # Bounds test
        start, end = reports.day_bounds_utc("2025-03-10", utc_offset_minutes=330)
        self.assertEqual(start, "2025-03-09T18:30:00Z")
        self.assertEqual(end, "2025-03-10T18:30:00Z")

        # Sales report for March 10 in India must include the morning sale
        res_mar10 = reports.daily_sales(self.conn, "2025-03-10", utc_offset_minutes=330)
        self.assertEqual(res_mar10["revenue_cents"], 1200)
        self.assertEqual(res_mar10["orders"], 1)

        # Sales report for March 9 in India must NOT include it
        res_mar9 = reports.daily_sales(self.conn, "2025-03-09", utc_offset_minutes=330)
        self.assertEqual(res_mar9["revenue_cents"], 0)

        # Also test negative offset (e.g. New York UTC-5 = -300 minutes)
        ny_start, ny_end = reports.day_bounds_utc("2025-03-10", utc_offset_minutes=-300)
        self.assertEqual(ny_start, "2025-03-10T05:00:00Z")
        self.assertEqual(ny_end, "2025-03-11T05:00:00Z")

    def test_price_change(self):
        self.app.handle("PUT", "/products/MUG", {"price_cents": 1500})
        self.assertEqual(self.app.catalog.get_product("MUG").price_cents, 1500)


if __name__ == "__main__":
    unittest.main()
