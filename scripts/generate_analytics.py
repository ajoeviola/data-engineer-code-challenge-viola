#!/usr/bin/env python3
"""Run the analytics queries against warehouse.db and write analytics.json.

Usage (from anywhere):
    python scripts/generate_analytics.py
    python scripts/generate_analytics.py --db data/warehouse.db --out output/analytics.json

Paths are relative to the project root (the folder above scripts/).
Uses only the Python standard library (sqlite3, json).

generated initially by llm prompt 5 
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = PROJECT_ROOT / "output" / "warehouse.db"
DEFAULT_OUT = PROJECT_ROOT / "output" / "analytics.json"

RECENT_WINDOW_DAYS = 30
RETURN_RATE_FLAG_PCT = 10.0
TOP_STORES = 5
TOP_CUSTOMERS = 10

# customer_id values treated as guest / anonymous (compared trimmed and upper-cased).
# A NULL or blank customer_id is always treated as a guest.
GUEST_CUSTOMER_IDS = ("GUEST", "ANONYMOUS", "ANON", "WALK-IN", "WALKIN", "UNKNOWN", "NONE", "N/A", "NA", "NULL")

# Columns returned as 0/1 by SQLite that should be true/false in the JSON.
BOOLEAN_COLUMNS = {"store_details_missing", "return_rate_over_threshold", "partial_month"}


GUEST_FILTER = (
    "customer_id IS NOT NULL AND TRIM(customer_id) <> '' "
    f"AND UPPER(TRIM(customer_id)) NOT IN ({', '.join('?' * len(GUEST_CUSTOMER_IDS))})"
)

QUERIES: dict[str, dict] = {
    # 1 ------------------------------------------------------------------------
    "top_stores_net_revenue_recent_30_days": {
        "description": f"Top {TOP_STORES} stores by net revenue in the most recent {RECENT_WINDOW_DAYS}-day "
                       "window of data. Returns reduce revenue: net revenue = sales + returns "
                       "(return amounts are negative). No rows are excluded.",
        "sql": f"""
            WITH bounds AS (
                SELECT date(MAX(d.full_date), '-{RECENT_WINDOW_DAYS - 1} days') AS window_start,
                       MAX(d.full_date)                                         AS window_end
                FROM fact_sales f JOIN dim_date d USING (date_key)
            )
            SELECT s.store_id,
                   s.store_name,
                   s.region,
                   s.store_details_missing,
                   ROUND(SUM(CASE WHEN f.is_return = 0 THEN f.total_amount ELSE 0 END), 2) AS gross_sales,
                   ROUND(SUM(CASE WHEN f.is_return = 1 THEN f.total_amount ELSE 0 END), 2) AS returns_amount,
                   ROUND(SUM(f.total_amount), 2)                                           AS net_revenue,
                   COUNT(*)                                                                AS transactions,
                   b.window_start,
                   b.window_end
            FROM fact_sales f
            JOIN dim_date  d USING (date_key)
            JOIN dim_store s USING (store_key)
            CROSS JOIN bounds b
            WHERE d.full_date BETWEEN b.window_start AND b.window_end
            GROUP BY s.store_key
            ORDER BY net_revenue DESC, s.store_id
            LIMIT {TOP_STORES}
        """,
        "params": (),
    },
    # 2 ------------------------------------------------------------------------
    "category_revenue_month_over_month": {
        "description": "Net revenue (sales + returns) by product category and month, with the change from "
                       "the previous month in %. Every category appears for every month (0 if no sales). "
                       "mom_change_pct is null for the first month and when the previous month's revenue "
                       "was 0. partial_month = true when the data doesn't cover the whole calendar month.",
        "sql": """
            WITH months AS (
                SELECT year, month,
                       printf('%04d-%02d', year, month) AS month_label,
                       COUNT(*) AS days_in_data,
                       CAST(julianday(date(printf('%04d-%02d-01', year, month), '+1 month'))
                          - julianday(printf('%04d-%02d-01', year, month)) AS INTEGER) AS days_in_month
                FROM dim_date
                GROUP BY year, month
            ),
            categories AS (SELECT DISTINCT category FROM dim_product),
            monthly AS (
                SELECT c.category, m.year, m.month, m.month_label,
                       m.days_in_data < m.days_in_month AS partial_month,
                       ROUND(COALESCE(SUM(f.total_amount), 0), 2) AS net_revenue
                FROM categories c
                CROSS JOIN months m
                LEFT JOIN dim_product p ON p.category = c.category
                LEFT JOIN dim_date   d ON d.year = m.year AND d.month = m.month
                LEFT JOIN fact_sales f ON f.product_key = p.product_key AND f.date_key = d.date_key
                GROUP BY c.category, m.year, m.month
            )
            SELECT category,
                   month_label AS month,
                   partial_month,
                   net_revenue,
                   LAG(net_revenue) OVER w AS previous_month_revenue,
                   ROUND(100.0 * (net_revenue - LAG(net_revenue) OVER w)
                         / NULLIF(ABS(LAG(net_revenue) OVER w), 0), 2) AS mom_change_pct
            FROM monthly
            WINDOW w AS (PARTITION BY category ORDER BY year, month)
            ORDER BY category, year, month
        """,
        "params": (),
    },
    # 3 ------------------------------------------------------------------------
    "return_rate_by_store": {
        "description": "Return rate by store = return transactions / total transactions (returns included "
                       f"in the total). return_rate_over_threshold = true when the rate is above "
                       f"{RETURN_RATE_FLAG_PCT:g}%.",
        "sql": f"""
            SELECT s.store_id,
                   s.store_name,
                   s.region,
                   s.store_details_missing,
                   COUNT(f.transaction_id)                                         AS total_transactions,
                   COALESCE(SUM(f.is_return), 0)                                   AS return_transactions,
                   ROUND(100.0 * SUM(f.is_return) / NULLIF(COUNT(f.transaction_id), 0), 2) AS return_rate_pct,
                   COALESCE(100.0 * SUM(f.is_return) / NULLIF(COUNT(f.transaction_id), 0)
                            > {RETURN_RATE_FLAG_PCT}, 0)                           AS return_rate_over_threshold
            FROM dim_store s
            LEFT JOIN fact_sales f USING (store_key)
            GROUP BY s.store_key
            ORDER BY return_rate_pct DESC, s.store_id
        """,
        "params": (),
    },
    # 4 ------------------------------------------------------------------------
    "avg_transaction_value_by_region": {
        "description": "Average transaction value by store region, excluding return transactions "
                       "(is_return = 1). Sales that were later returned are included.",
        "sql": """
            SELECT s.region,
                   COUNT(*)                       AS transactions,
                   ROUND(SUM(f.total_amount), 2)  AS total_sales,
                   ROUND(AVG(f.total_amount), 2)  AS avg_transaction_value
            FROM fact_sales f
            JOIN dim_store s USING (store_key)
            WHERE f.is_return = 0
            GROUP BY s.region
            ORDER BY avg_transaction_value DESC, s.region
        """,
        "params": (),
    },
    # 5 ------------------------------------------------------------------------
    "top_customers_lifetime_spend": {
        "description": f"Top {TOP_CUSTOMERS} customers by lifetime spend, excluding guest/anonymous "
                       "transactions (blank customer_id or one of the guest IDs listed under definitions). "
                       "lifetime_spend = net (sales + returns). transaction_count = purchase transactions; "
                       "return_count = return transactions; avg_order_value = average purchase amount "
                       "(returns not counted as orders).",
        "sql": f"""
            SELECT customer_id,
                   ROUND(SUM(total_amount), 2)                                      AS lifetime_spend,
                   SUM(CASE WHEN is_return = 0 THEN 1 ELSE 0 END)                   AS transaction_count,
                   SUM(is_return)                                                   AS return_count,
                   ROUND(AVG(CASE WHEN is_return = 0 THEN total_amount END), 2)     AS avg_order_value
            FROM fact_sales
            WHERE {GUEST_FILTER}
            GROUP BY customer_id
            ORDER BY lifetime_spend DESC, customer_id
            LIMIT {TOP_CUSTOMERS}
        """,
        "params": GUEST_CUSTOMER_IDS,
    },
}


def run_query(con: sqlite3.Connection, sql: str, params: tuple) -> list[dict]:
    cur = con.execute(sql, params)
    cols = [c[0] for c in cur.description]
    rows = []
    for values in cur.fetchall():
        row = dict(zip(cols, values))
        for col in BOOLEAN_COLUMNS & row.keys():
            row[col] = None if row[col] is None else bool(row[col])
        rows.append(row)
    return rows


def scalar(con: sqlite3.Connection, sql: str, params: tuple = ()):
    return con.execute(sql, params).fetchone()[0]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate analytics.json from warehouse.db")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args(argv)

    if not args.db.is_file():
        print(f"database not found: {args.db.resolve()} (run load_warehouse.ipynb first)", file=sys.stderr)
        return 1

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)  # read-only
    try:
        results = {}
        for name, q in QUERIES.items():
            rows = run_query(con, q["sql"], q["params"])
            results[name] = {"description": q["description"], "row_count": len(rows),
                             "sql": " ".join(q["sql"].split()), "rows": rows}
            print(f"{name}: {len(rows)} rows", file=sys.stderr)

        # Pull the 30-day window up next to the result it belongs to.
        top = results["top_stores_net_revenue_recent_30_days"]
        top["window"] = ({"start": top["rows"][0]["window_start"], "end": top["rows"][0]["window_end"],
                          "days": RECENT_WINDOW_DAYS} if top["rows"] else None)
        for row in top["rows"]:
            row.pop("window_start"), row.pop("window_end")

        flagged = [r["store_id"] for r in results["return_rate_by_store"]["rows"] if r["return_rate_over_threshold"]]
        results["return_rate_by_store"]["flagged_stores"] = flagged

        guest_txn = scalar(con, f"SELECT COUNT(*) FROM fact_sales WHERE NOT ({GUEST_FILTER})", GUEST_CUSTOMER_IDS)
        results["top_customers_lifetime_spend"]["guest_transactions_excluded"] = guest_txn

        report = {
            "metadata": {
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "database": str(args.db.resolve().relative_to(PROJECT_ROOT)
                                if args.db.resolve().is_relative_to(PROJECT_ROOT) else args.db),
                "sqlite_version": sqlite3.sqlite_version,
                "data_start_date": scalar(con, "SELECT MIN(d.full_date) FROM fact_sales JOIN dim_date d USING (date_key)"),
                "data_end_date": scalar(con, "SELECT MAX(d.full_date) FROM fact_sales JOIN dim_date d USING (date_key)"),
                "fact_sales_rows": scalar(con, "SELECT COUNT(*) FROM fact_sales"),
            },
            "definitions": {
                "net_revenue": "SUM(total_amount) over all transactions; returns have negative amounts, so they reduce it.",
                "return_transaction": "fact_sales.is_return = 1 (negative quantity).",
                "recent_window": f"The last {RECENT_WINDOW_DAYS} days of data, ending on the latest transaction date (inclusive).",
                "return_rate_flag_threshold_pct": RETURN_RATE_FLAG_PCT,
                "guest_customer_ids": ["NULL / blank", *GUEST_CUSTOMER_IDS],
                "placeholder_stores": "store_details_missing = true: store has sales but no record in the stores file (details 'Unknown').",
            },
            "results": results,
        }
    finally:
        con.close()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    print(f"wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
