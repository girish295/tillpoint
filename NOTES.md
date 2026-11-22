# NOTES

Fill in every field below before you submit. We read this file first: a submission with an empty field is not graded.
Write in your own words. If you did not fix something, set Status to `not fixed` and say what you found and tried; that still counts.
Status is one of: `fixed`, `partly fixed`, `not fixed`. Commits: the short hashes of the commits for this item, or `none`.

## 1. Report

> Finance says that on some days the revenue in the daily report is higher than what the payment provider paid out, and a few orders show more money received than their total.

**Status:** fixed

**What I saw:** When the same payment webhook event (e.g. `evt_dup_1`) was delivered more than once, each delivery created a new row in the `payments` table. This inflated the recorded revenue for the day and caused the order's total recorded payment (`paid_cents`) to exceed the order's actual total amount.

**Root cause:** Lack of idempotency handling in `Payments.handle_event()`. The `payments` table schema in `tillpoint/db.py` lacked a `UNIQUE` constraint on `event_id`, and `handle_event` in `tillpoint/payments.py` blindly executed `INSERT INTO payments` on every incoming webhook payload without checking if the `event_id` had already been processed.

**Fix and why:** Added a `UNIQUE` constraint to `event_id` in the `payments` table definition in `tillpoint/db.py` to enforce database-level integrity. In `tillpoint/payments.py`, updated `handle_event` to first query `SELECT * FROM payments WHERE event_id = ?`. If an existing record is found, it immediately returns the current order status without re-inserting or double-counting, ensuring strict idempotency and returning HTTP 200 to the payment gateway so it ceases retries.

**How I verified it:** Added `test_duplicate_payment_webhook_is_idempotent` in `tests/test_orders.py`. Verified that on the original code, `paid_cents` became 2400 instead of 1200 (`AssertionError: 2400 != 1200`). Verified that after the fix, repeated deliveries of the exact same event keep `paid_cents` at 1200, return the correct status ("paid"), and only 1 row exists in `payments`.

**Commits:** 14f568e

## 2. Report

> Products show as sold out, or nearly, while the shelf is full. Staff say it gets worse on days when many customers abandon checkout.

**Status:** fixed

**What I saw:** When customers created orders, stock was decremented immediately. If a customer abandoned checkout or if a pending order was cancelled via `/orders/<id>/cancel`, the reserved stock was never returned to the catalog, leaving available inventory depleted even when physical shelves were full.

**Root cause:** In `Orders.cancel` (`tillpoint/orders.py`), stock restoration was guarded by `if order["status"] == "paid":`. However, `Orders.create()` reserves inventory when the order is created in `'pending'` status. Consequently, cancelling any `'pending'` order skipped the stock release loop entirely.

**Fix and why:** Changed the condition in `Orders.cancel()` from `if order["status"] == "paid":` to `if order["status"] in ("pending", "paid"):`. This guarantees that reserved inventory is returned to catalog stock whenever any unfulfilled order (whether pending or paid) is cancelled. Repeated cancellations are safe because an order already in `'cancelled'` status returns early without re-releasing stock.

**How I verified it:** Added `test_cancel_pending_order_restocks` in `tests/test_orders.py`. On original code, stock remained 1 instead of restoring to 3 (`AssertionError: 1 != 3`). After the fix, cancelling a pending order correctly restores stock to 3, and calling cancel a second time does not double-restore stock.

**Commits:** 4b66866

## 3. Report

> Customers in taxed regions who used a coupon say the tax on their receipt is too high. Receipts without a coupon are right.

**Status:** fixed

**What I saw:** On orders with both coupons and regional sales tax, the tax calculated on the receipt was noticeably higher than expected. Orders without coupons calculated tax correctly.

**Root cause:** In `tillpoint/pricing.py`, `price_order()` evaluated `tax = tax_for(sub, region)` using the gross subtotal `sub` before evaluating `discount = discount_for(coupon, sub)`. Tax was calculated on the full pre-discount amount rather than the net taxable amount after subtracting the discount.

**Fix and why:** Reordered calculations in `price_order()`: calculate `discount` first, compute the net taxable amount `taxable = max(0, sub - discount)`, and pass `taxable` into `tax_for(taxable, region)`. This ensures customers are taxed only on the discounted total they actually pay.

**How I verified it:** Added `test_coupon_with_tax_calculates_tax_after_discount` in `tests/test_orders.py` (1 MUG @ 1200 cents, 10% coupon = 120 cents discount, CA tax @ 7.25%). On original code, tax was 87 cents (tax on 1200); after fix, tax is 78 cents (tax on 1080), total is 1158 cents (`AssertionError: 87 != 78` before fix, passes after fix).

**Commits:** bcb8768

## 4. Report

> Our shop in India (UTC+5:30) says sales taken in the morning do not appear in that day's report and turn up under the day before.

**Status:** fixed

**What I saw:** In our Indian shop (UTC+5:30), sales made during local morning hours (e.g. 7:30 AM IST) did not appear in that day's sales report and instead showed up in the previous calendar day's report.

**Root cause:** In `tillpoint/reports.py`, `day_bounds_utc()` calculated the UTC starting instant of a local calendar day using `start += timedelta(minutes=utc_offset_minutes)`. Because India is UTC+5:30 (+330 minutes), local midnight corresponds to 18:30 UTC on the previous day (`00:00 - 05:30`). Adding the positive offset set `start` to 05:30 UTC (11:00 AM IST), completely excluding all sales between midnight and 11:00 AM from the daily report.

**Fix and why:** Changed the offset calculation in `day_bounds_utc()` to subtract the offset: `start -= timedelta(minutes=utc_offset_minutes)`. This correctly maps local midnight to UTC across all timezones: UTC+5:30 starts at 18:30 UTC previous day, UTC+0 starts at 00:00 UTC, and negative offsets (e.g. UTC-5 New York = -300 minutes) start at 05:00 UTC on the current day.

**How I verified it:** Added `test_india_morning_sales_in_correct_day_report` in `tests/test_orders.py`. Verified boundary instants for UTC+5:30 (`2025-03-09T18:30:00Z` to `2025-03-10T18:30:00Z`) and UTC-5 (`2025-03-10T05:00:00Z` to `2025-03-11T05:00:00Z`). Confirmed an India morning sale at 02:00 UTC (7:30 AM IST) on March 10 is correctly reported on 2025-03-10 and excluded from 2025-03-09.

**Commits:** f2aea2e

## 5. Report

> Ever since we started running two server workers, we have sold more units of an item than we had, and its stock went negative.

**Status:** fixed

**What I saw:** When running multiple concurrent server workers, units sold exceeded total inventory and catalog stock dropped into negative numbers.

**Root cause:** In `tillpoint/catalog.py`, `Catalog.reserve()` performed a non-atomic "Check-Then-Act" (TOCTOU) sequence: it fetched product stock (reading stale in-memory cached data from `self._cache`), checked `product.stock < qty` in Python, and then issued an unconditional `UPDATE products SET stock = stock - ?`. Under concurrent workers, both workers read available stock before either decremented it, causing overselling and negative inventory.

**Fix and why:** Refactored `Catalog.reserve()` to use an atomic conditional SQL update: `UPDATE products SET stock = stock - ? WHERE sku = ? AND stock >= ?`. SQLite acquires a write lock during the statement; if available stock is less than `qty`, `cur.rowcount` is 0. If `cur.rowcount == 0`, we query the database to get current stock and raise `OutOfStock(sku, qty, current_stock)` (or `UnknownProduct` if missing). In addition, invalidation of `self._cache` ensures stale stock counts are not retained.

**How I verified it:** Added `test_concurrent_workers_prevent_negative_stock` in `tests/test_orders.py` simulating two worker instances against a shared SQLite database with 5 units of stock. Verified that on original code, both workers succeeded in reserving 4 units without raising `OutOfStock` (producing negative stock). On fixed code, the second worker raises `OutOfStock` and database stock remains positive at 1.

**Commits:** ea10548

## 6. Open question

> Open question from support, no single right answer: sometimes a customer really does pay twice for one order (two separate payments, not a retry of the same one). Today the system records both and says nothing. Decide what should happen, build the smallest version of your decision, and explain what it costs in `NOTES.md`.

**My decision:** Option B: Record the second payment and flag the order as `overpaid`, unless the order has already been marked `shipped`, in which case retain the `'shipped'` status to preserve warehouse fulfillment tracking.

**Why:**
1. Financial truth: The payment gateway captured real customer money. Rejecting or ignoring the webhook would create an accounting discrepancy between actual bank settlements and our database records.
2. Visibility: Marking status as `'overpaid'` provides immediate visibility to support and finance teams to trigger a customer refund or credit for the surplus amount `(paid_cents - total_cents)`.
3. Fulfillment safety: If an order has already shipped, overwriting status with `'overpaid'` would erase shipping history and confuse warehouse operations. Keeping `'shipped'` preserves fulfillment integrity while payments records prove the overpayment.
4. Operational continuity: Updated `Orders.ship()` to permit shipping `'overpaid'` orders so warehouse operations are not halted while support processes refunds.

**What it costs:**
1. Requires support or finance intervention to reconcile and issue the excess refund back to the customer.
2. Downstream queries (`Orders.ship`, `Orders.cancel`, `reports.top_products`) must recognize `'overpaid'` as a valid active order status.
3. For already-shipped orders, customer support must query payment totals against order totals to detect overpayment, as top-level status remains `'shipped'`.

**What I built:**
- Updated `Payments.handle_event()` in `tillpoint/payments.py` to record all genuine payment events. When total payments exceed `order["total_cents"]`, if status is not `'shipped'`, update status to `'overpaid'`. If already `'shipped'`, keep `'shipped'`.
- Updated `Orders.ship()` in `tillpoint/orders.py` to allow shipping orders with `'overpaid'` status.
- Updated `Orders.cancel()` in `tillpoint/orders.py` to safely restock items if an `'overpaid'` order is cancelled.
- Updated `reports.top_products()` in `tillpoint/reports.py` to include `'overpaid'` orders in top seller metrics.

**How I verified it:** Added `test_genuine_double_payment_flags_overpaid_and_preserves_shipped` in `tests/test_orders.py`. Verified that two 1200-cent payments for a 1200-cent order record 2400 cents, update status to `'overpaid'`, allow subsequent shipping, and that a secondary payment arriving after an order is already `'shipped'` preserves the `'shipped'` status while capturing the full 2400 cents.

**Commits:** 810be7a

## Other things I noticed

Problems outside the reports: what, where, and whether you fixed them. Write `none` if there were none.

1. ResourceWarning for unclosed SQLite connections in `tests/test_orders.py`: The tests previously opened multiple connections in helper calls (such as in `test_daily_sales`) without explicitly closing them, emitting ResourceWarnings in Python 3.13. Managed connection lifecycles in new tests with `finally: conn.close()`.
2. Integer Currency Representation: All prices, discounts, and payments are strictly integer cents throughout the codebase. `round_half_up` in `pricing.py` uses integer division `(2 * numerator + denominator) // (2 * denominator)`, which avoids floating point precision issues.
3. PRAGMA Foreign Keys: `PRAGMA foreign_keys = ON` is enforced on every database connection, ensuring database-level integrity across `orders`, `order_items`, and `payments`.

## Session logs

Which AI tools you used, and one line per file in `session_logs/`.

- Claude Code: `session_logs/claude_session.md` - Primary pair-programming session covering project architecture, reproduction test development, root-cause investigations, and code diff reviews for all 6 reports.
- ChatGPT: `session_logs/chatgpt_session.md` - Technical review session discussing the concurrency edge cases for Report 5 and evaluating business tradeoffs between Option A and Option B for Report 6.
