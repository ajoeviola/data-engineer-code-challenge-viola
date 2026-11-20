# Retail Data Pipeline

This pipeline profiles, cleans and models three raw retail exports (stores, products and transactions) into a SQLite star schema, then answers five business questions from that warehouse. The raw data came from legacy systems with little governance, so every change to it is a deliberate decision, logged below with its reasoning.

## Run it (inside scripts folder directory)

```bash
pip install -r requirements.txt
python pipeline.py          # profile -> clean -> load -> analytics
pytest tests/
```

Requires Python 3.9+. To regenerate the raw data, run `python scripts/seed_data.py`. To run part of the pipeline, use `python pipeline.py --only clean` or `python pipeline.py --from load`.

| Output                                                           | Produced by                                                 |
| ---------------------------------------------------------------- | ----------------------------------------------------------- |
| `output/profiling_report.json`                                   | Profiling (Part 1)                                          |
| `data/validated/*.csv`, `cleaning_log.csv`, files for the client | Cleaning (Part 2)                                           |
| `output/warehouse.db`                                            | Loading (Part 3)                                            |
| `output/analytics.json`                                          | Analytics (Part 4)                                          |
| `output/notebooks/*.ipynb`                                       | Executed copies of the notebooks, with every check's result |

## Architecture

```
 data/raw/*.csv  (never modified)
      │
      ├──► profiler.py ─────────────► output/profiling_report.json
      │
      ▼
 notebooks/data_cleaning.ipynb ────────► data/validated/*.csv
      │   every rule checks its expected count; any mismatch stops the run
      ▼
 notebooks/load_warehouse.ipynb ───────► output/warehouse.db
      │   star schema with PKs, FKs and CHECK constraints
      ▼
 scripts/generate_analytics.py ────────► output/analytics.json
      (SQL queries)

 pipeline.py runs all four steps in order.  tests/ covers profiling, cleaning and analytics.
```

| Path                             | Purpose                                                                     |
| -------------------------------- | --------------------------------------------------------------------------- |
| `profiler.py`                    | Generic `profile(df, name, key_cols=None, as_of=None)` plus the file loader |
| `notebooks/data_cleaning.ipynb`  | Cleaning, documented step by step, with a decision log                      |
| `notebooks/load_warehouse.ipynb` | Builds and verifies the star schema                                         |
| `scripts/generate_analytics.py`  | Runs the analytics SQL and writes the JSON                                  |
| `docs/warehouse_schema.md`       | Full schema reference: every column, a diagram, and example queries         |
| `pipeline.py`                    | Runs the whole pipeline                                                     |

**Why notebooks for cleaning and loading:** each cleaning decision sits next to the data it affects, with the rows shown before and after. That makes the decisions easy to review. `pipeline.py` runs the notebooks without modifying them, so the pipeline is still a single command.

## Part 1: Profiling

`profile()` doesn't depend on table or column names, so it works on any DataFrame. Files are read with every value as raw text, so problems like leading zeros, `$12.50` or `"N/A"` reach the profiler unchanged instead of being converted silently by pandas.

**Table level**

- Row and column counts
- Exact duplicate rows
- Key analysis: duplicated keys, and which columns conflict between the duplicates
- Columns that could be keys (100% unique) and columns that hold only one value
- A list of plain-language warnings

**Column level**

- Nulls, counting empty and whitespace-only strings as null
- Placeholder nulls (`N/A`, `null`, `none`, …), counted separately
- Leading or trailing whitespace, leading zeros and trailing zeros
- The inferred type, with the % of values that fit it

**Type inference:** a column counts as numeric or date when at least 90% of its populated values fit that type. That way a few bad values don't hide the column's real type, and those values are listed in the warnings.

- **Numeric columns:** min, max, mean, std, and counts of zeros, negatives, currency symbols and thousands separators
- **Date columns:**
  - Min and max date, and dates later than today
  - The number of values in each date format, with impossible dates such as `2026-02-30` counted separately
  - Day/month ambiguity detected for each column

## Part 2: Data quality findings and decisions

Each rule checks the count found during profiling. If the data changes, the pipeline stops at that rule instead of cleaning data the decision wasn't made for. `cleaning_log.csv` records the counts from each run.

| Issue                                                                                             | File         | Count                   | Decision                                                                                                                                         | Rationale                                                                                                                                                                                                                                         |
| ------------------------------------------------------------------------------------------------- | ------------ | ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `region` blank for both OR stores                                                                 | stores       | 2                       | Filled with "West"                                                                                                                               | Based on the other state/region columns, we can impute the region "West".                                                                                                                                                                         |
| `S007` appears twice with different `store_name`s                                                 | stores       | 2 rows (1 dropped)      | Dropped one row (kept the first)                                                                                                                 | It's the same record.                                                                                                                                                                                                                             |
| Invalid zip codes                                                                                 | stores       | 2                       | Kept `zip_code` as text; flagged zips that aren't 5 digits in `zip_code_invalid`; not imputed                                                    | Text avoids truncating leading/trailing zeros. It's too risky to impute new zips without being sure, but if zip code isn't necessary for analytics, it can be safe to keep in the data for now.                                                   |
| Stores with transactions but no store record (S016–S019)                                          | stores       | 4 stores (5 txns)       | Added placeholder stores (details `Unknown`, `store_details_missing = 1`); sales kept; listed for the client                                     | Instead of dropping these records and losing data, we can just add the stores to the stores table and keep the transactions                                                                                                                       |
| Products with no `category`                                                                       | products     | 5                       | Filled from other products with the same `supplier_id`                                                                                           | Each supplier maps to exactly one category.                                                                                                                                                                                                       |
| Exact duplicate rows                                                                              | products     | 1                       | Dropped                                                                                                                                          | exact duplicates can be safely dropped                                                                                                                                                                                                            |
| `P005` appears twice with different prices                                                        | products     | 2 rows (1 dropped)      | Kept the row whose price matches the transactions                                                                                                | Only one matches the price in the transactions table.                                                                                                                                                                                             |
| `P027` catalog price is 0.00                                                                      | products     | 1                       | Replaced with the price from its transactions                                                                                                    | It has a price in the transactions table.                                                                                                                                                                                                         |
| Products with transactions but no product record (P031, P032)                                     | products     | 2 products (3 txns)     | Added placeholder products (category and supplier `Unknown`, no catalog price, `product_details_missing = 1`); sales kept; listed for the client | Instead of dropping these records and osing the data, we can just add the product keys to the DIM table                                                                                                                                           |
| `product_name` column                                                                             | products     | all rows                | Not loaded into the warehouse                                                                                                                    | It's just the `product_id` with "product" at the start as a prefix.                                                                                                                                                                               |
| Exact duplicate rows                                                                              | transactions | 15                      | Deduplicated                                                                                                                                     | exact dupes can be safely dropped                                                                                                                                                                                                                 |
| `quantity` and `total_amount` both 0                                                              | transactions | 5                       | Dropped; saved to a file for the client                                                                                                          | We drop them so they don't skew average transaction value or revenue tracking.                                                                                                                                                                    |
| Negative quantities are returns matching an earlier sale (same store, product, customer and date) | transactions | all negatives (5 count) | Flagged both the return and its sale (`exclude_from_metrics`); kept                                                                              | So they aren't counted in revenue or other metrics, but aren't dropped, so we can still track them as needed.                                                                                                                                     |
| `$` in `total_amount`                                                                             | transactions | 25                      | Removed `$`, cast to a decimal number                                                                                                            | _[add rationale]_                                                                                                                                                                                                                                 |
| Mixed date formats; 7 values where day and month are both ≤ 12                                    | transactions | 7 values                | Cast to `YYYY-MM-DD`; each ambiguous value resolved individually                                                                                 | The dates range from March to June: `01-04-2026` (TXN10011) is April 1st; `04/12/2026` and `04/01/2026` are April; `05/07/2026` is May; `12-03-2026` is March; `05/05/2026` can go either way. That leaves `06-04-2026`, which is June 4th, 2026. |

**Principles behind these decisions**

- **Raw data is never modified.** The cleaning reads `data/raw` and writes everything to `data/validated`.
- **Nothing is imputed without evidence.** Zip codes are flagged rather than guessed. Ambiguous dates are only resolved from an explicit list, and a new ambiguous value stops the run.
- **Rows are dropped only when they'd distort metrics.** Otherwise the record is kept and flagged: returns, invalid zips, and stores or products with no record.
- **Dropped or questionable records are saved for the client:**
  - `transactions_dropped_zero_quantity.csv`
  - `stores_missing_from_source.csv`
  - `products_missing_from_source.csv`

## Part 3: Warehouse schema

```
                 dim_date
                    │ date_key
 dim_store ─── fact_sales ─── dim_product
  store_key        │            product_key
                   └─ matched_transaction_id (return ↔ sale)
```

| Table                       | Grain                                                             | Key columns                                                                                                                                                                                          |
| --------------------------- | ----------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `fact_sales`                | One row per transaction (returns are their own rows)              | `transaction_id`, `date_key`, `store_key`, `product_key`, `customer_id`, `quantity`, `unit_price`, `total_amount`, `is_return`, `is_returned_sale`, `matched_transaction_id`, `exclude_from_metrics` |
| `dim_date`                  | One row per calendar day, first to last transaction date, no gaps | `date_key` (YYYYMMDD), year, quarter, month, weekday, ISO week, `is_weekend`                                                                                                                         |
| `dim_store`                 | One row per store                                                 | `store_key`, `store_id`, name, city, state, `zip_code` (text), `zip_code_invalid`, `region`, `store_details_missing`                                                                                 |
| `dim_product`               | One row per product                                               | `product_key`, `product_id`, `category`, `supplier_id`, `catalog_unit_price`, `product_details_missing`                                                                                              |
| `v_sales_reportable` (view) | `fact_sales` where `exclude_from_metrics = 0`                     |                                                                                                                                                                                                      |

Every column is described in [`docs/warehouse_schema.md`](docs/warehouse_schema.md).

**Modeling decisions**

- **Surrogate keys:** each dimension has an integer surrogate key, and the natural ID is kept as a `UNIQUE` column. `transaction_id` and `customer_id` stay on the fact table as plain IDs. There's no customer table, because the source has no customer attributes.
- **Constraints:** primary keys, foreign keys and `CHECK` constraints are declared, so SQLite itself rejects bad rows. For example, quantity can't be 0, and a catalog price must be above 0 except on placeholder products.
- **Safe rebuilds:** the loader rebuilds the database from scratch in a temporary file and only replaces `warehouse.db` once every check has passed.
- **Products with more than one price on record:**
  - P005: kept the catalog price that matches the transactions.
  - P027: 0.00 replaced with the transaction price.
  - Placeholder products have no catalog price, because P031 was sold at two different prices, so no single price could be chosen.
  - Catalog price (`dim_product.catalog_unit_price`) and charged price (`fact_sales.unit_price`) are stored separately. Revenue always uses the charged amount.
- **Returns:**
  - Kept as negative-quantity rows, linked one-to-one with the sale they reverse through `matched_transaction_id`.
  - When there are several candidate sales, the one with the same quantity is preferred, then the earliest.
  - Both the return and the sale are flagged with `exclude_from_metrics`, and the whole sale is flagged even for a partial return.
- **Excluded from the warehouse:**
  - Duplicate rows: the second S007 row, product and transaction exact duplicates, and the wrong P005 row.
  - The 5 zero-quantity transactions.
  - The `product_name` column.
  - The reasons are in the table above.

## Part 4: Analytics

**Why SQL instead of pandas:**

- The questions are asked of the warehouse itself, so they also test that the model works for real queries.
- SQL is portable and easy to review, and SQLite's window functions handle the month-over-month calculation.
- Each result in `analytics.json` includes the exact SQL that produced it, alongside the definitions used.

| #   | Question                                    | Definition used                                                                                                                                                                                                |
| --- | ------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Top 5 stores by net revenue, last 30 days   | The window is the 30 days ending on the latest transaction date. Net revenue = sales + returns, since return amounts are negative, so returns reduce revenue.                                                  |
| 2   | Month-over-month revenue change by category | Uses net revenue. Every category appears in every month, with 0 if it had no sales. The change is null for the first month or when the previous month was 0. Partly covered months are marked `partial_month`. |
| 3   | Return rate by store                        | Return transactions ÷ all transactions. Stores above 10% are flagged and listed in `flagged_stores`.                                                                                                           |
| 4   | Average transaction value by region         | Return rows are excluded. Sales that were later returned are kept.                                                                                                                                             |
| 5   | Top 10 customers by lifetime spend          | Guests are a blank `customer_id` or a guest ID such as `GUEST`, and they're excluded. Spend is net of returns. Transaction count and average order value cover purchases only.                                 |

Placeholder stores and products appear as `Unknown` in region and category results, so totals stay complete.

## Part 5: Tests

`pytest tests/` runs 9 tests, 3 per area, each asserting exact expected values:

- **Profiling:**
  - An empty DataFrame
  - An all-null column, where None, NaN, `""` and whitespace count as null
  - Currency text such as `$10.00` and `N/A` still inferred as numeric, with the counts right
- **Cleaning:** these use the functions defined in the notebook, so the real code is tested.
  - Known dates parse correctly, including the ambiguous-date decisions
  - An ambiguous date that isn't on the list is refused, not guessed
  - `$` amounts are rejected until the `$` is removed
- **Analytics:** a 5-transaction fixture is loaded into SQLite using the real warehouse DDL, then checked for:
  - Net revenue in the 30-day window: a return reduces it, and an older sale falls outside the window
  - The return-rate flag
  - Guest exclusion

## Productionizing

- **Orchestration:** run each step as a task in Airflow, Dagster or Prefect, using the same four-step order. Each step's output becomes the next step's input, with retries and alerts on failure. The cleaning and loading logic would move from notebooks into modules (see below).
- **Incremental loads:**
  - Load transactions incrementally by date, using a watermark and upserts on `transaction_id`, instead of rebuilding everything.
  - Track store and product changes over time as slowly changing dimensions (type 2), so history isn't overwritten.
  - Hold back transactions that arrive before their store or product does, until the dimension row exists.
- **Observability:**
  - The count checks already stop the run. In production they'd also publish metrics and alerts: rows in and out per step, rows dropped or flagged, and placeholder-row counts.
  - Profile every new raw file and compare it against the last one, so changes like a new date format or a jump in nulls are caught early.
- **Data contracts:** agree a schema and formats (ISO dates, numeric amounts, a valid store and product on every sale) with the source system owners, so these issues are fixed at the source.

## With more time

- Move the cleaning and loading logic out of the notebooks into importable modules, and keep the notebooks as thin documentation on top. Tests could then import functions directly instead of reading notebook cells.
- Add an end-to-end test that runs the full pipeline on a small fixture of raw files and checks the validated output.
- Move the expected counts from code into a config file, so a new data drop means updating a file, not code.
- Store money as integer cents, to avoid floating-point rounding.
- Clean `opened_date` in stores, which is loaded as received.
- Resolve the open questions with the client: the real details for S016–S019 and P031/P032, the correct zip codes, and whether a partial return should flag the whole sale.
