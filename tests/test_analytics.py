"""Tests for scripts/generate_analytics.py against a tiny hand-built warehouse.

Fixture (latest date 2026-06-20, so the 30-day window is 2026-05-22 to 2026-06-20):

  txn  date        store  customer  amount
  T1   2026-06-01  S001   C1          30   sale, returned by T2
  T2   2026-06-01  S001   C1         -10   return
  T3   2026-06-15  S002   C2         100
  T4   2026-06-20  S002   (guest)     50
  T5   2026-04-01  S001   C3         500   outside the 30-day window
"""
import json
import sqlite3

import pytest

import generate_analytics

FACTS = [  # transaction_id, date, store_key, customer_id, total_amount, is_return, is_returned_sale, matched
    ("T1", "2026-06-01", 1, "C1", 30.0, 0, 1, "T2"),
    ("T2", "2026-06-01", 1, "C1", -10.0, 1, 0, "T1"),
    ("T3", "2026-06-15", 2, "C2", 100.0, 0, 0, None),
    ("T4", "2026-06-20", 2, None, 50.0, 0, 0, None),
    ("T5", "2026-04-01", 1, "C3", 500.0, 0, 0, None),
]


@pytest.fixture(scope="module")
def results(tmp_path_factory, warehouse_defs):
    tmp = tmp_path_factory.mktemp("analytics")
    db, out = tmp / "warehouse.db", tmp / "analytics.json"

    con = sqlite3.connect(db)
    con.executescript(warehouse_defs["DDL"])  # the real warehouse schema
    with con:
        for d in sorted({f[1] for f in FACTS}):
            y, m, day = map(int, d.split("-"))
            con.execute("INSERT INTO dim_date VALUES (?, ?, ?, ?, ?, 'Month', ?, 1, 'Monday', 1, 0)",
                        (int(d.replace("-", "")), d, y, (m - 1) // 3 + 1, m, day))
        for key, store_id in [(1, "S001"), (2, "S002")]:
            con.execute("INSERT INTO dim_store VALUES (?, ?, 'Store', 'City', 'ST', '00000', 0, 'East', NULL, 0)",
                        (key, store_id))
        con.execute("INSERT INTO dim_product VALUES (1, 'P001', 'Tools', 'SUP1', 10.0, 0)")
        for tid, d, store, cust, amt, ret, rsale, matched in FACTS:
            qty = -1 if ret else 1
            con.execute("INSERT INTO fact_sales VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (tid, int(d.replace("-", "")), store, cust, qty, abs(amt), amt,
                         ret, rsale, matched, int(ret or rsale)))
    con.close()

    assert generate_analytics.main(["--db", str(db), "--out", str(out)]) == 0
    return json.loads(out.read_text())["results"]


def test_top_stores_net_revenue_last_30_days(results):
    r = results["top_stores_net_revenue_recent_30_days"]

    assert r["window"]["start"] == "2026-05-22"
    # S002: 100 + 50. S001: 30 - 10 (the return reduces revenue); T5 is outside the window.
    assert [(x["store_id"], x["net_revenue"]) for x in r["rows"]] == [("S002", 150.0), ("S001", 20.0)]


def test_return_rate_by_store_flags_over_10_percent(results):
    rows = {x["store_id"]: x for x in results["return_rate_by_store"]["rows"]}

    assert rows["S001"]["return_rate_pct"] == 33.33   # 1 return out of 3 transactions
    assert rows["S002"]["return_rate_pct"] == 0.0
    assert results["return_rate_by_store"]["flagged_stores"] == ["S001"]


def test_top_customers_exclude_guests(results):
    r = results["top_customers_lifetime_spend"]

    assert [(x["customer_id"], x["lifetime_spend"]) for x in r["rows"]] == [
        ("C3", 500.0), ("C2", 100.0), ("C1", 20.0)]
    assert r["guest_transactions_excluded"] == 1
