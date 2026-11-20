"""Tests for profile() in profiler.py."""
import pandas as pd

from profiler import profile


def test_empty_dataframe():
    df = pd.DataFrame({"a": pd.Series([], dtype=object)})

    result = profile(df, "empty")

    assert result["table"]["row_count"] == 0
    assert result["columns"]["a"]["null_pct"] == 0.0
    assert "table has no rows" in result["warnings"]


def test_all_null_column():
    df = pd.DataFrame({"id": ["1", "2", "3", "4"], "notes": [None, float("nan"), "", "   "]}, dtype=object)

    result = profile(df, "t")

    assert result["columns"]["notes"]["null_count"] == 4
    assert result["columns"]["notes"]["null_pct"] == 100.0
    assert result["table"]["all_null_columns"] == ["notes"]


def test_currency_text_is_inferred_as_numeric():
    amounts = ["$10.00", "$5.50", "3.25", "4.00", "1.50", "2.00", "6.00", "7.00", "8.00", "N/A"]
    df = pd.DataFrame({"total_amount": amounts}, dtype=object)

    col = profile(df, "t")["columns"]["total_amount"]

    assert col["inferred_type"] == "decimal"
    assert col["value_checks"]["currency_symbol_count"] == 2
    assert col["placeholder_null_count"] == 1
    assert (col["numeric"]["min"], col["numeric"]["max"]) == (1.5, 10.0)
