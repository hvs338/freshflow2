# Meridian Markets — 90-day extract

A 10-store pilot slice of Meridian Markets, 2026-04-01 through 2026-06-30.
Three files. Everything joins on `item_id` and `store_id`.

| File | Rows | Grain |
|---|---|---|
| `items.csv` | 200 | one row per item |
| `shipments.csv` | 42,550 | one row per store-item-shipment |
| `sales_daily.csv` | 181,124 | one row per store-item-day with movement |

## items.csv

| Column | Type | Notes |
|---|---|---|
| `item_id` | int | Primary key. Joins to both fact files. |
| `description` | text | Item name as merchandising refers to it. |
| `dept` | text | Produce, Dairy, Meat, Bakery, Deli, Grocery. |
| `category` | text | Sub-grouping within a department. |
| `unit_of_measure` | text | `LB` or `EA`. The unit that `units_sold` is counted in. |
| `case_size` | int | Units per case. Correct for every item — use it to convert shipment cases to units. |
| `unit_cost` | decimal | Meridian's cost per unit, in dollars. Stable over the window. |

## shipments.csv

| Column | Type | Notes |
|---|---|---|
| `date` | date | Date the shipment arrived at the store. |
| `store_id` | int | 101–110. |
| `banner` | text | Meridian Foods or GreenLeaf. |
| `region` | text | Northeast, Midwest, or West. |
| `item_id` | int | Joins to `items.csv`. |
| `cases_received` | int | Cases, **not units**. Units received = `cases_received * case_size`. |

## sales_daily.csv

| Column | Type | Notes |
|---|---|---|
| `date` | date | Date of sale. |
| `store_id` | int | 101–110. |
| `banner` | text | Denormalized from the store. |
| `region` | text | Denormalized from the store. |
| `item_id` | int | Joins to `items.csv`. |
| `units_sold` | int | Units, in the item's `unit_of_measure`. |
| `net_sales` | decimal | Retail dollars for that store-item-day. |

Store-item-days with no movement are absent rather than zero-filled.

## How Meridian computes shrink

There is no shrink table. Shrink is derived: **units shipped minus units sold** over
a period. Units shipped comes from `cases_received * case_size`.

Two things about that definition are genuinely ambiguous inside Meridian, and both
matter for anything you build.

**1. Units or cost.** Shrink can be counted in units, or in dollars by multiplying
those units by `unit_cost`. The two do not move together. A month can show unit
shrink up sharply while cost shrink is flat, because the mix moved toward cheaper
items. "Is shrink up?" has two defensible answers, and they lead to different
decisions.

**2. What's in scope.** When the ops team says "shrink" they mean the five fresh
departments (Produce, Dairy, Meat, Bakery, Deli). Grocery is center store, where
shipped-minus-sold is mostly inventory build rather than waste. Including it or
excluding it changes the number materially.

Neither ambiguity has a single right answer. Both are real, and how your system
handles them is a design decision.

## Questions Meridian's team actually asks

Representative, not a checklist. You are not expected to support all of these —
depth on a few beats shallow coverage of all.

1. What were our top 10 items by shrink last month?
2. Is shrink up or down versus the prior month?
3. Which stores have the worst shrink?
4. Why is dairy shrink up in the Northeast in June?
5. What are our best-selling items in Produce?
6. How did strawberry sales trend over the three months?
7. Which department has the highest shrink rate?
8. What was our total shrink cost in June?
9. How do our two banners compare on shrink?
10. Which items have high unit shrink but little cost impact?
