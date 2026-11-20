"""Tests for the cleaning rules defined in notebooks/data_cleaning.ipynb."""
import pandas as pd
import pytest


def test_dates_parse_to_expected_values(cleaning_defs):
    parse = cleaning_defs["parse_transaction_date"]

    assert parse("2026-03-15")[0] == pd.Timestamp("2026-03-15")   # already ISO
    assert parse("25-03-2026")[0] == pd.Timestamp("2026-03-25")   # day > 12, so it must be the day
    assert parse("04/25/2026")[0] == pd.Timestamp("2026-04-25")
    assert parse("01-04-2026")[0] == pd.Timestamp("2026-04-01")   # decision: TXN10011 is April 1st
    assert parse("06-04-2026")[0] == pd.Timestamp("2026-06-04")   # decision: June 4th, 2026


def test_unlisted_ambiguous_date_is_not_guessed(cleaning_defs):
    assert cleaning_defs["parse_transaction_date"]("03/04/2026") == (None, "ambiguous: NOT resolved")


def test_dollar_amounts_must_have_dollar_removed(cleaning_defs):
    to_number = cleaning_defs["to_number"]
    amounts = pd.Series(["$10.00", "5.50"])

    with pytest.raises(ValueError):
        to_number(amounts, "total_amount")
    assert to_number(amounts.str.replace("$", "", regex=False), "total_amount").tolist() == [10.0, 5.5]
