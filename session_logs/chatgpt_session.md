# ChatGPT Session Log - Architecture & Tradeoff Review

**Date:** 2026-11-21  
**Tool:** ChatGPT (GPT-4o)  
**Candidate:** Girish  

---

### User Prompt
> I am reviewing the architecture of Tillpoint, a small Python/SQLite order management backend for a retail store.
> Users have reported concurrency problems when multiple worker processes run: stock went negative when customers purchased items simultaneously.
> Can you review the concurrency model in SQLite and how process-level caching interacts with database writes?

### ChatGPT Response
In Python web applications running multiple worker processes (such as Gunicorn, uWSGI, or multiple HTTPServer instances), each process maintains its own separate memory address space.

Here is the architectural analysis of what happens:

1. **In-Memory Caches across Worker Processes:**
   If a worker caches product records (including current stock) in a dictionary like `self._cache[sku] = product`, that dictionary is private to that process.
   When Worker A receives an order and decrements stock in SQLite, it can only clear its own `_cache`. Worker B has no shared memory mechanism to know Worker A updated the database. Worker B continues reading stale stock numbers from its local memory.

2. **Check-Then-Act (TOCTOU) Race Condition:**
   Even without caching, if code executes:
   ```python
   product = get_product(sku) # Step 1: Check
   if product.stock < qty:
       raise OutOfStock()
   conn.execute("UPDATE products SET stock = stock - ? WHERE sku = ?", (qty, sku)) # Step 2: Act
   ```
   Both workers can execute Step 1 concurrently before either reaches Step 2. If available stock is 5 and both request 4:
   - Worker 1 checks: 5 >= 4 (OK)
   - Worker 2 checks: 5 >= 4 (OK)
   - Worker 1 decrements by 4 -> stock becomes 1
   - Worker 2 decrements by 4 -> stock becomes -3!

3. **The Solution: Atomic Conditional Update:**
   SQLite supports single-statement atomic updates with row-level condition checking:
   ```sql
   UPDATE products SET stock = stock - ? WHERE sku = ? AND stock >= ?
   ```
   During execution, SQLite acquires a reserved/exclusive lock. The condition `stock >= ?` is evaluated atomically inside the database engine.
   - If stock is sufficient, the update modifies 1 row (`cursor.rowcount == 1`).
   - If stock is insufficient, 0 rows are modified (`cursor.rowcount == 0`).
   Checking `cur.rowcount == 0` tells you immediately whether the reservation succeeded or failed, without any race window.

---

### User Prompt
> That makes complete sense. We will implement the atomic conditional update with `WHERE stock >= ?` in `Catalog.reserve()`.
>
> Now I want your perspective on Report 6, which is an open design question:
> "Sometimes a customer really does pay twice for one order (two separate payments, not a retry of the same one). Today the system records both and says nothing. Decide what should happen, build the smallest version of your decision, and explain what it costs in NOTES.md."
>
> What are the advantages and disadvantages of:
> Option A: Rejecting the second payment.
> Option B: Recording the payment and marking the order as overpaid.

### ChatGPT Response
This is a classic e-commerce payment reconciliation problem. Here is an objective comparison:

#### Option A: Reject the Payment
- **How it works:** In `handle_event()`, if `order.status in ('paid', 'shipped')`, raise an exception or return a 4xx/reject response.
- **Pros:**
  - Very simple implementation.
  - Prevents the internal system from having surplus balances on orders.
- **Cons & Risks (Major):**
  - Payment webhooks are asynchronous notifications of events that have *already happened in the real world*. The customer's credit card was already charged.
  - If your system rejects the webhook, the payment gateway considers delivery failed and retries repeatedly.
  - Your financial records will show less money than what actually landed in your merchant bank account, creating an accounting discrepancy.

#### Option B: Record the Payment and Flag as `overpaid`
- **How it works:** Always insert the payment record into the `payments` table. If total payments exceed `order.total_cents`, transition order status to `'overpaid'`.
- **Pros:**
  - Preserves exact financial truth: every cent received is auditable in the database.
  - The `'overpaid'` status serves as an explicit operational trigger for customer support and finance to issue a refund for the excess amount.
- **Cons & Tradeoffs:**
  - Requires customer support / finance workflow to process refunds.
  - Downstream queries must be updated so `'overpaid'` orders are treated properly (e.g. allowing shipping, including in sales reporting).

#### Critical Edge Case: Shipped Orders
Pay close attention to what happens if an order is already marked `'shipped'`:
If a customer makes a duplicate payment after the item has already shipped from the warehouse, changing the order status to `'overpaid'` would destroy the fact that the order was shipped!
The warehouse might think the order is unfulfilled, or customer support wouldn't know the package is already in transit.
Therefore, the recommended logic is:
- If `status == 'shipped'`: Record the payment, but leave `status` as `'shipped'`.
- If `status != 'shipped'`: Record the payment and update `status` to `'overpaid'`.
- Allow `Orders.ship()` to ship an `'overpaid'` order so fulfillment is not blocked while waiting for financial reconciliation.

---

### User Prompt
> Excellent analysis. I will choose Option B and implement the shipped status preservation guard. Thank you!
