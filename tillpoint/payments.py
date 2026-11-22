"""Payment provider webhooks. The provider retries a delivery until it gets a 2xx, so events can repeat."""
from . import db


class PaymentError(Exception):
    pass


class Payments:
    def __init__(self, conn):
        self.conn = conn

    def handle_event(self, event, now=None):
        """event: dict(id, type, order_id, amount_cents). Returns the order's status after the event."""
        if event.get("type") != "payment.succeeded":
            return None
        existing = self.conn.execute("SELECT * FROM payments WHERE event_id = ?", (event["id"],)).fetchone()
        if existing is not None:
            order = self.conn.execute("SELECT status FROM orders WHERE id = ?", (existing["order_id"],)).fetchone()
            return order["status"] if order else None
        order = self.conn.execute("SELECT * FROM orders WHERE id = ?", (event["order_id"],)).fetchone()
        if order is None:
            raise PaymentError(f"payment for unknown order {event['order_id']}")
        if order["status"] == "cancelled":
            raise PaymentError(f"order {order['id']} is cancelled")
        self.conn.execute(
            "INSERT INTO payments (order_id, event_id, amount_cents, received_at) VALUES (?, ?, ?, ?)",
            (order["id"], event["id"], event["amount_cents"], db.to_iso(now or db.utcnow())),
        )
        total_paid = self.paid_cents(order["id"])
        current_status = order["status"]
        if current_status == "pending" and total_paid == order["total_cents"]:
            self.conn.execute("UPDATE orders SET status = 'paid' WHERE id = ?", (order["id"],))
            return "paid"
        elif total_paid > order["total_cents"]:
            if current_status != "shipped":
                self.conn.execute("UPDATE orders SET status = 'overpaid' WHERE id = ?", (order["id"],))
                return "overpaid"
            return "shipped"
        return current_status

    def paid_cents(self, order_id):
        row = self.conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS paid FROM payments WHERE order_id = ?", (order_id,)
        ).fetchone()
        return row["paid"]
