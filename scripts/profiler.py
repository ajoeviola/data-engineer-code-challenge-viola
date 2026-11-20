#The profiler was initially generated from LLM result 2. in the transcript file. 
#!/usr/bin/env python3
"""Generic data profiler.

Profiles any tabular file without knowing its table or column names. Every
column is read as raw text, so the profiler sees values exactly as they are in
the file (leading zeros, currency symbols, "N/A" strings, mixed date formats)
and infers the most likely type itself.

Usage
-----
    python profiler.py stores.csv transactions.csv products.csv \
        --key transactions=transaction_id --key stores=store_id \
        --as-of 2026-11-20

With no files given, every *.csv / *.tsv / *.txt file in ./data is profiled.
Table names come from the file names (transactions.csv -> "transactions").
The combined report is written to output/profiling_report.json.

Or from Python:
    from profiler import profile
    result = profile(df, "transactions", key_cols=["transaction_id"])

Only pandas and the Python standard library are used. The data is never
modified.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import platform
import re
import sys
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

PROFILER_VERSION = "1.0.0"

# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
TYPE_THRESHOLD = 0.90  # share of populated values that must fit a type
NEAR_TYPE_THRESHOLD = 0.50  # above this but below TYPE_THRESHOLD -> "mixed" warning
TOP_N = 5  # number of top values to report
MAX_EXAMPLES = 5  # number of example values or keys in findings

# Strings that mean "missing". Matched case-insensitively after stripping whitespace.
PLACEHOLDER_NULLS = frozenset({
    "n/a", "na", "n.a.", "nan", "null", "(null)", "nul", "none", "nil",
    "-", "--", "---", "?", "??", ".", "missing", "unknown", "undefined",
    "not available", "not applicable", "tbd", "blank", "(blank)", "empty",
    "#n/a", "#na", "#null!", "#value!", "#ref!", "#div/0!", "nat",
})

BOOLEAN_VALUES = frozenset({"true", "false", "yes", "no", "y", "n", "t", "f"})

CURRENCY_SYMBOLS = "$€£¥₹₩₽¢"
CURRENCY_CODES = ("USD", "EUR", "GBP", "CAD", "AUD", "NZD", "JPY", "CNY",
                  "INR", "CHF", "MXN", "BRL", "ZAR", "SEK", "NOK", "DKK")
_SYM = re.escape(CURRENCY_SYMBOLS)
_CODES = "|".join(CURRENCY_CODES)
_CURRENCY_RE = re.compile(rf"[{_SYM}]|\b(?:{_CODES})\b", re.IGNORECASE)
_LEADING_CURRENCY_RE = re.compile(rf"^\(?[+-]?\s*(?:[{_SYM}]|(?:{_CODES})\b)", re.IGNORECASE)
_NUMBER_RE = r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?"


# --------------------------------------------------------------------------
# Date patterns
# --------------------------------------------------------------------------
def _prep_iso(s: pd.Series) -> pd.Series:
    """'2024-01-15T10:30:00Z' -> '2024-01-15 10:30:00' (time zone dropped)."""
    s = s.str.replace(r"(?:Z|[+-]\d{2}:?\d{2})$", "", regex=True)
    return s.str.replace("T", " ", regex=False)


def _prep_day_month_name(s: pd.Series) -> pd.Series:
    """'15-Jan-2024' / '15/Jan/24' -> '15 Jan 2024' / '15 Jan 24'."""
    s = s.str.replace(r"[-/,.]", " ", regex=True)
    return s.str.replace(r"\s+", " ", regex=True).str.strip()


def _prep_month_name_day(s: pd.Series) -> pd.Series:
    """'Jan. 15th, 2024' -> 'Jan 15 2024'."""
    s = s.str.replace(r"(\d)(?:st|nd|rd|th)\b", r"\1", regex=True)
    s = s.str.replace(r"[.,]", " ", regex=True)
    return s.str.replace(r"\s+", " ", regex=True).str.strip()


# Each pattern: label, full-match regex, strptime formats, and options.
#   ambiguous: the first two numbers could be month/day or day/month. The
#              order is settled per column, and {a}/{b} in the label become
#              MM/DD or DD/MM.
#   compact:   YYYYMMDD looks just like an 8-digit integer, so it never makes a
#              column a date on its own (see _infer_type).
_DATE_PATTERNS: list[dict[str, Any]] = [
    dict(label="YYYY-MM-DD hh:mm:ss",
         regex=r"\d{4}-\d{1,2}-\d{1,2}[T ]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?",
         formats=("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"),
         prep=_prep_iso),
    dict(label="YYYY-MM-DD", regex=r"\d{4}-\d{1,2}-\d{1,2}", formats=("%Y-%m-%d",)),
    dict(label="YYYY/MM/DD", regex=r"\d{4}/\d{1,2}/\d{1,2}", formats=("%Y/%m/%d",)),
    dict(label="YYYY.MM.DD", regex=r"\d{4}\.\d{1,2}\.\d{1,2}", formats=("%Y.%m.%d",)),
    dict(label="{a}/{b}/YYYY hh:mm",
         regex=r"(\d{1,2})/(\d{1,2})/\d{4}\s+\d{1,2}:\d{2}(?::\d{2})?(?:\s?[AaPp][Mm])?",
         formats=("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y %I:%M:%S %p",
                  "%m/%d/%Y %I:%M %p", "%m/%d/%Y %I:%M:%S%p", "%m/%d/%Y %I:%M%p"),
         ambiguous=True),
    dict(label="{a}/{b}/YYYY", regex=r"(\d{1,2})/(\d{1,2})/\d{4}", formats=("%m/%d/%Y",), ambiguous=True),
    dict(label="{a}-{b}-YYYY", regex=r"(\d{1,2})-(\d{1,2})-\d{4}", formats=("%m-%d-%Y",), ambiguous=True),
    dict(label="{a}.{b}.YYYY", regex=r"(\d{1,2})\.(\d{1,2})\.\d{4}", formats=("%m.%d.%Y",), ambiguous=True),
    dict(label="{a}/{b}/YY", regex=r"(\d{1,2})/(\d{1,2})/\d{2}", formats=("%m/%d/%y",), ambiguous=True),
    dict(label="DD-Mon-YYYY", regex=r"\d{1,2}[-/ ][A-Za-z]{3,9}\.?[-/ ,]+\d{2,4}",
         formats=("%d %b %Y", "%d %B %Y", "%d %b %y", "%d %B %y"), prep=_prep_day_month_name),
    dict(label="Mon DD, YYYY", regex=r"[A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}",
         formats=("%b %d %Y", "%B %d %Y"), prep=_prep_month_name_day),
    dict(label="YYYYMMDD", regex=r"(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])",
         formats=("%Y%m%d",), compact=True),
]


def _swap_day_month(fmt: str) -> str:
    return fmt.replace("%m", "\0").replace("%d", "%m").replace("\0", "%d")


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def _pct(part: float, whole: float) -> float:
    return round(100.0 * part / whole, 2) if whole else 0.0


def _wsum(weights: pd.Series, mask: pd.Series | None = None) -> int:
    """Number of rows behind a set of unique values."""
    return int(weights.sum() if mask is None else weights[mask.to_numpy()].sum())


def _examples(values: pd.Series, weights: pd.Series, mask: pd.Series, n: int = MAX_EXAMPLES) -> list[str]:
    """The most frequent unique values where mask is True."""
    m = mask.to_numpy()
    sub = pd.DataFrame({"v": values[m].to_numpy(), "w": weights[m].to_numpy()})
    return sub.sort_values("w", ascending=False)["v"].head(n).tolist()


def _fmt_examples(items: list[Any]) -> str:
    return ", ".join(repr(x) for x in items)


def _to_jsonable(obj: Any) -> Any:
    """Recursively convert numpy/pandas objects into plain JSON-safe Python."""
    if obj is None or obj is pd.NaT or obj is pd.NA:
        return None
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_to_jsonable(v) for v in obj]
    if isinstance(obj, (pd.Timestamp, datetime, date)):
        return obj.isoformat()
    if hasattr(obj, "item") and not isinstance(obj, (str, bytes)):  # numpy scalar
        obj = obj.item()
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


def _as_text(series: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Return (text, null_mask).

    text holds every value as a str ('' where missing). null_mask is True for
    real missing values and for empty or whitespace-only strings.
    """
    is_na = series.isna()
    text = series.astype(object).where(is_na, series.astype(str))
    text = text.where(~is_na, "").astype(object)
    null_mask = is_na | text.str.strip().eq("")
    return text, null_mask


# --------------------------------------------------------------------------
# Value parsers (they run on the column's unique values, not on every row)
# --------------------------------------------------------------------------
def _parse_numeric(u: pd.Series) -> dict[str, pd.Series]:
    """Try to read each value as a number. Also flags currency symbols,
    thousands separators, % signs and (negative) parentheses."""
    t = u
    paren = t.str.fullmatch(r"\(.*\)")
    t = t.where(~paren, t.str[1:-1].str.strip())
    currency = t.str.contains(_CURRENCY_RE)
    leading_currency = t.str.contains(_LEADING_CURRENCY_RE)
    t = t.str.replace(_CURRENCY_RE, "", regex=True).str.strip()
    t = t.str.replace(r"^([+-])\s+", r"\1", regex=True)
    t = t.str.replace(r"^([+-])\1+", r"\1", regex=True)
    thousands = t.str.fullmatch(r"[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?")
    t = t.where(~thousands, t.str.replace(",", "", regex=False))
    percent = t.str.fullmatch(r".+%")
    t = t.where(~percent, t.str[:-1].str.strip())
    is_num = t.str.fullmatch(_NUMBER_RE)
    values = pd.to_numeric(t.where(is_num), errors="coerce")
    values = values.where(~paren, -values.abs())
    return dict(
        cleaned=t, is_num=is_num, values=values, currency=currency,
        leading_currency=leading_currency, thousands=thousands & is_num,
        percent=percent & is_num, parentheses=paren & is_num,
        has_decimal=is_num & t.str.contains(r"[.eE]"),
    )


def _parse_dates(u: pd.Series) -> dict[str, Any]:
    """Match each value against the known date patterns, then parse it.

    Day/month order for ambiguous patterns (01/02/2024) is decided per column:
    a first number above 12 means DD/MM, a second number above 12 means MM/DD.
    If neither happens, MM/DD is assumed and flagged.
    """
    key = pd.Series([None] * len(u), index=u.index, dtype=object)
    for i, p in enumerate(_DATE_PATTERNS):
        m = key.isna() & u.str.fullmatch(p["regex"])
        key[m] = i

    # Settle day/month order using every ambiguous value in the column.
    amb_idx = [i for i, p in enumerate(_DATE_PATTERNS) if p.get("ambiguous")]
    amb_mask = key.isin(amb_idx)
    order = None  # None = no ambiguous values in this column
    if amb_mask.any():
        parts = u[amb_mask].str.extract(r"^(\d{1,2})\D(\d{1,2})\D").astype(int)
        first_gt12 = bool((parts[0] > 12).any())
        second_gt12 = bool((parts[1] > 12).any())
        if first_gt12 and second_gt12:
            order = "conflicting"
        elif first_gt12:
            order = "DD/MM"
        elif second_gt12:
            order = "MM/DD"
        else:
            order = "assumed MM/DD"
    day_first = order == "DD/MM"

    parsed = pd.Series(pd.NaT, index=u.index, dtype="datetime64[ns]")
    labels = pd.Series([None] * len(u), index=u.index, dtype=object)
    for i, p in enumerate(_DATE_PATTERNS):
        m = key.eq(i)
        if not m.any():
            continue
        sub = u[m]
        if p.get("prep"):
            sub = p["prep"](sub)
        formats = [_swap_day_month(f) for f in p["formats"]] if (p.get("ambiguous") and day_first) else p["formats"]
        out = pd.Series(pd.NaT, index=sub.index, dtype="datetime64[ns]")
        for fmt in formats:
            todo = out.isna()
            if not todo.any():
                break
            out[todo] = pd.to_datetime(sub[todo], format=fmt, errors="coerce")
        parsed[m] = out
        label = p["label"]
        if p.get("ambiguous"):
            a, b = ("DD", "MM") if day_first else ("MM", "DD")
            label = label.format(a=a, b=b)
        labels[m] = label

    matched = key.notna()
    return dict(
        matched=matched, valid=matched & parsed.notna(), parsed=parsed, labels=labels,
        compact=key.eq(len(_DATE_PATTERNS) - 1), order=order,
    )


# --------------------------------------------------------------------------
# Column profiling
# --------------------------------------------------------------------------
def _infer_type(n_pop: int, w: pd.Series, num: dict, dates: dict, is_bool: pd.Series) -> tuple[str, float, dict]:
    num_w = _wsum(w, num["is_num"])
    # Date-shaped values count toward the date type even when they are not
    # real dates (2024-02-30); those are reported as invalid dates instead.
    date_w = _wsum(w, dates["matched"])
    date_noncompact_w = _wsum(w, dates["matched"] & ~dates["compact"])
    bool_w = _wsum(w, is_bool)
    text_w = _wsum(w, ~(num["is_num"] | dates["matched"] | is_bool))
    fit = {"numeric": _pct(num_w, n_pop), "date": _pct(date_w, n_pop),
           "boolean": _pct(bool_w, n_pop), "text_only": _pct(text_w, n_pop)}
    t = TYPE_THRESHOLD * 100
    if n_pop == 0:
        return "empty", 0.0, fit
    if fit["numeric"] >= t and _pct(date_noncompact_w, n_pop) < t:
        kind = "decimal" if _wsum(w, num["has_decimal"]) else "integer"
        return kind, fit["numeric"], fit
    if fit["date"] >= t:
        return "date", fit["date"], fit
    if fit["boolean"] >= t:
        return "boolean", fit["boolean"], fit
    return "text", fit["text_only"], fit


def _profile_column(col: str, series: pd.Series, as_of: date, warnings: list[str]) -> dict:
    n = len(series)
    text, null_mask = _as_text(series)
    is_text_source = series.dtype == object or isinstance(series.dtype, pd.StringDtype)

    raw = text[~null_mask]  # non-null, as stored
    stripped = raw.str.strip()
    is_placeholder = stripped.str.lower().isin(PLACEHOLDER_NULLS)
    raw_pop = raw[~is_placeholder]
    pop = stripped[~is_placeholder]  # real values, whitespace stripped

    null_count = int(null_mask.sum())
    empty_string_count = int((~series.isna() & null_mask).sum())
    placeholder_count = int(is_placeholder.sum())
    placeholder_values = raw[is_placeholder].value_counts().head(10).to_dict()
    n_pop = len(pop)

    # Work on unique values with their row counts. Much faster on large tables.
    vc = pop.value_counts()
    u = pd.Series(vc.index.astype(str), dtype=object)
    w = pd.Series(vc.to_numpy())

    num = _parse_numeric(u)
    dates = _parse_dates(u)
    is_bool = u.str.lower().isin(BOOLEAN_VALUES)
    inferred, inferred_pct, fit = _infer_type(n_pop, w, num, dates, is_bool)

    distinct = int(raw.nunique())
    top = raw.value_counts().head(TOP_N)

    # Raw-text checks (meaningful only when the source holds text)
    whitespace = int((raw != stripped).sum()) if is_text_source else 0
    leading_zero = _wsum(w, num["cleaned"].str.fullmatch(r"[+-]?0\d+(?:\.\d+)?")) if is_text_source else 0
    trailing_zero = _wsum(w, num["cleaned"].str.fullmatch(r"[+-]?\d*\.\d*0")) if is_text_source else 0
    currency = _wsum(w, num["currency"])
    leading_currency = _wsum(w, num["leading_currency"])

    out: dict[str, Any] = {
        "source_dtype": str(series.dtype),
        "inferred_type": inferred,
        "inferred_type_pct": inferred_pct,
        "type_fit_pct": fit,
        "row_count": n,
        "null_count": null_count,
        "null_pct": _pct(null_count, n),
        "empty_string_count": empty_string_count,
        "placeholder_null_count": placeholder_count,
        "placeholder_null_pct": _pct(placeholder_count, n),
        "placeholder_values": placeholder_values,
        "total_missing_count": null_count + placeholder_count,
        "total_missing_pct": _pct(null_count + placeholder_count, n),
        "populated_count": n_pop,
        "distinct_count": distinct,
        "distinct_pct": _pct(distinct, n),
        "is_unique": n > 0 and null_count == 0 and distinct == n,
        "is_constant": distinct == 1,
        "top_values": [{"value": k, "count": int(v)} for k, v in top.items()],
        "value_checks": {
            "leading_or_trailing_whitespace_count": whitespace,
            "leading_zero_count": leading_zero,
            "trailing_zero_count": trailing_zero,
            "currency_symbol_count": currency,
            "leading_currency_symbol_count": leading_currency,
            "thousands_separator_count": _wsum(w, num["thousands"]),
            "percent_sign_count": _wsum(w, num["percent"]),
            "parentheses_negative_count": _wsum(w, num["parentheses"]),
        },
        "numeric": None,
        "date": None,
        "text": None,
        "nonconforming_count": 0,
        "nonconforming_examples": [],
    }

    # Warnings for every column type
    if n and null_count == n:
        warnings.append(f"{col}: column is entirely null/empty")
    elif null_count:
        extra = f" (incl. {empty_string_count} empty/whitespace-only strings)" if empty_string_count else ""
        warnings.append(f"{col}: {null_count} null values ({_pct(null_count, n)}%){extra}")
    if placeholder_count:
        warnings.append(f"{col}: {placeholder_count} placeholder null values "
                        f"({_fmt_examples(list(placeholder_values)[:MAX_EXAMPLES])})")
    if whitespace:
        warnings.append(f"{col}: {whitespace} values have leading or trailing whitespace")
    if currency:
        lead = f", {leading_currency} with a leading currency symbol" if leading_currency else ""
        ex = _examples(u, w, num["currency"], 3)
        warnings.append(f"{col}: {currency} values have a currency symbol{lead} (e.g. {_fmt_examples(ex)})")
    if leading_zero:
        ex = _examples(u, w, num["cleaned"].str.fullmatch(r"[+-]?0\d+(?:\.\d+)?"), 3)
        warnings.append(f"{col}: {leading_zero} values have leading zeros (e.g. {_fmt_examples(ex)}); "
                        "converting to a number would drop them")
    if distinct == 1 and n > 1:
        warnings.append(f"{col}: holds only one value ({top.index[0]!r})")

    # Type-specific sections
    if inferred in ("integer", "decimal"):
        out["numeric"] = _numeric_stats(w, num)
        bad = ~num["is_num"]
        out["nonconforming_count"] = _wsum(w, bad)
        out["nonconforming_examples"] = _examples(u, w, bad)
        ns = out["numeric"]
        if out["nonconforming_count"]:
            warnings.append(f"{col}: {out['nonconforming_count']} non-numeric values in a numeric column "
                            f"(e.g. {_fmt_examples(out['nonconforming_examples'])})")
        if ns["negative_count"]:
            warnings.append(f"{col}: {ns['negative_count']} negative values")
        if ns["thousands_separator_count"]:
            warnings.append(f"{col}: {ns['thousands_separator_count']} values use a thousands separator")
        if _pct(_wsum(w, dates["valid"]), n_pop) >= TYPE_THRESHOLD * 100:
            warnings.append(f"{col}: every value also reads as a YYYYMMDD date; could be a date stored as a number")
    elif inferred == "date":
        out["date"] = _date_stats(u, w, dates, as_of)
        bad = ~dates["valid"]
        out["nonconforming_count"] = _wsum(w, bad)
        out["nonconforming_examples"] = _examples(u, w, bad)
        ds = out["date"]
        if len(ds["format_counts"]) > 1:
            fmts = ", ".join(f"{k}: {v}" for k, v in ds["format_counts"].items())
            warnings.append(f"{col}: {len(ds['format_counts'])} different date formats ({fmts})")
        if ds["invalid_date_count"]:
            warnings.append(f"{col}: {ds['invalid_date_count']} values look like dates but are not real dates "
                            f"(e.g. {_fmt_examples(ds['invalid_date_examples'])})")
        other = out["nonconforming_count"] - ds["invalid_date_count"]
        if other:
            ex = _examples(u, w, ~dates["matched"])
            warnings.append(f"{col}: {other} non-date values in a date column (e.g. {_fmt_examples(ex)})")
        if ds["future_date_count"]:
            warnings.append(f"{col}: {ds['future_date_count']} dates are after the as-of date {as_of.isoformat()}")
        if ds["day_month_order"] == "conflicting":
            warnings.append(f"{col}: day/month order is inconsistent (some values only work as DD/MM, others as MM/DD)")
        elif ds["day_month_order"] == "assumed MM/DD":
            warnings.append(f"{col}: day/month order is ambiguous (no value has a day above 12); assumed MM/DD")
    elif inferred == "boolean":
        bad = ~is_bool
        out["nonconforming_count"] = _wsum(w, bad)
        out["nonconforming_examples"] = _examples(u, w, bad)
        variants = sorted(set(u[is_bool]))
        if len(variants) > 2:
            warnings.append(f"{col}: boolean column uses {len(variants)} spellings ({_fmt_examples(variants)})")
    elif inferred == "text":
        out["text"] = _text_stats(u, w, raw_pop)
        if out["text"]["case_or_whitespace_variant_groups"]:
            warnings.append(f"{col}: {out['text']['case_or_whitespace_variant_groups']} values appear with different "
                            f"case/spacing (e.g. {_fmt_examples(out['text']['variant_examples'][0])})")
        t_pct = TYPE_THRESHOLD * 100
        for kind, fits in (("numeric", num["is_num"]), ("date", dates["matched"])):
            if NEAR_TYPE_THRESHOLD * 100 <= fit[kind] < t_pct:
                ex = _examples(u, w, ~fits)
                warnings.append(f"{col}: {fit[kind]}% of values are {kind}, below the {t_pct:.0f}% needed "
                                f"to treat it as {kind} (other values e.g. {_fmt_examples(ex)})")
    return out


def _numeric_stats(w: pd.Series, num: dict) -> dict:
    m = num["is_num"].to_numpy()
    v = num["values"][m]
    ww = w[m]
    cnt = int(ww.sum())
    mean = float((v * ww).sum() / cnt) if cnt else None
    std = float(math.sqrt((ww * (v - mean) ** 2).sum() / (cnt - 1))) if cnt > 1 else None
    return {
        "numeric_count": cnt,
        "negative_count": int(ww[(v < 0).to_numpy()].sum()),
        "zero_count": int(ww[(v == 0).to_numpy()].sum()),
        "min": float(v.min()) if cnt else None,
        "max": float(v.max()) if cnt else None,
        "mean": round(mean, 6) if mean is not None else None,
        "std": round(std, 6) if std is not None else None,
        "currency_symbol_count": _wsum(w, num["currency"]),
        "leading_currency_symbol_count": _wsum(w, num["leading_currency"]),
        "thousands_separator_count": _wsum(w, num["thousands"]),
        "percent_sign_count": _wsum(w, num["percent"]),
    }


def _date_stats(u: pd.Series, w: pd.Series, dates: dict, as_of: date) -> dict:
    valid = dates["valid"]
    parsed = dates["parsed"][valid]
    pw = w[valid.to_numpy()]
    fmt_counts = (pd.DataFrame({"f": dates["labels"][dates["matched"]], "w": w[dates["matched"].to_numpy()]})
                  .groupby("f")["w"].sum().sort_values(ascending=False))
    invalid = dates["matched"] & ~valid
    future = parsed.dt.normalize() > pd.Timestamp(as_of)
    max_d = parsed.max() if len(parsed) else None
    return {
        "date_count": int(pw.sum()),
        "min_date": parsed.min().date().isoformat() if len(parsed) else None,
        "max_date": max_d.date().isoformat() if max_d is not None else None,
        "days_from_max_date_to_as_of": (as_of - max_d.date()).days if max_d is not None else None,
        "format_counts": {k: int(v) for k, v in fmt_counts.items()},
        "distinct_format_count": len(fmt_counts),
        "day_month_order": dates["order"],
        "invalid_date_count": _wsum(w, invalid),
        "invalid_date_examples": _examples(u, w, invalid),
        "future_date_count": int(pw[future.to_numpy()].sum()),
        "pre_1900_count": int(pw[(parsed < pd.Timestamp("1900-01-01")).to_numpy()].sum()),
    }


def _text_stats(u: pd.Series, w: pd.Series, raw_pop: pd.Series) -> dict:
    lengths = u.str.len()
    cnt = int(w.sum())
    uniq_raw = pd.Series(raw_pop.unique(), dtype=object)
    norm = uniq_raw.str.strip().str.lower()
    groups = uniq_raw.groupby(norm).agg(list)
    variants = groups[groups.map(len) > 1]
    return {
        "min_length": int(lengths.min()) if cnt else None,
        "max_length": int(lengths.max()) if cnt else None,
        "mean_length": round(float((lengths * w).sum() / cnt), 2) if cnt else None,
        "case_or_whitespace_variant_groups": int(len(variants)),
        "variant_examples": [sorted(v)[:MAX_EXAMPLES] for v in variants.head(MAX_EXAMPLES)],
    }


# --------------------------------------------------------------------------
# Table-level checks
# --------------------------------------------------------------------------
def _key_analysis(df: pd.DataFrame, key_cols: list[str], warnings: list[str]) -> dict:
    missing = [c for c in key_cols if c not in df.columns]
    if missing:
        warnings.append(f"key column(s) not found: {', '.join(missing)}")
        return {"key_cols": key_cols, "error": f"columns not found: {missing}"}

    label = "+".join(key_cols)
    null_key = pd.Series(False, index=df.index)
    for c in key_cols:
        null_key |= _as_text(df[c])[1]

    dup_mask = df.duplicated(subset=key_cols, keep=False)
    dups = df[dup_mask]
    other = [c for c in df.columns if c not in key_cols]
    n_dup_keys = n_conflict = 0
    examples: list[dict] = []
    if len(dups):
        grouped = dups.groupby(key_cols, dropna=False, sort=False)
        n_dup_keys = grouped.ngroups
        if other:
            nun = grouped[other].nunique(dropna=False)
            conflict = nun[(nun > 1).any(axis=1)]
            n_conflict = len(conflict)
            for key, row in conflict.head(MAX_EXAMPLES).iterrows():
                key_t = key if isinstance(key, tuple) else (key,)
                diff_cols = row[row > 1].index.tolist()
                sel = pd.Series(True, index=dups.index)
                for c, k in zip(key_cols, key_t):
                    sel &= dups[c].isna() if pd.isna(k) else dups[c].eq(k)
                rows = dups[sel]
                examples.append({
                    "key": dict(zip(key_cols, key_t)),
                    "row_count": len(rows),
                    "differing_columns": diff_cols,
                    "values": {c: rows[c].drop_duplicates().head(MAX_EXAMPLES).tolist() for c in diff_cols},
                })

    if null_key.any():
        warnings.append(f"key {label}: {int(null_key.sum())} rows have a null key")
    if n_dup_keys:
        warnings.append(f"key {label}: {n_dup_keys} key values are duplicated ({int(dup_mask.sum())} rows), "
                        f"{n_conflict} of them with conflicting values in other columns")
    return {
        "key_cols": key_cols,
        "null_key_row_count": int(null_key.sum()),
        "duplicated_key_count": n_dup_keys,
        "rows_with_duplicated_key": int(dup_mask.sum()),
        "conflicting_key_count": n_conflict,
        "identical_duplicate_key_count": n_dup_keys - n_conflict,
        "conflict_examples": examples,
    }


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
def profile(df: pd.DataFrame, name: str, key_cols: list[str] | None = None,
            as_of: date | None = None) -> dict:
    """Profile one DataFrame. Works for any table: nothing about its columns is assumed.

    key_cols: optional column(s) expected to identify a row uniquely.
    as_of:    reference date for future-date checks (default: today).

    Best run on data read as text (dtype=str, keep_default_na=False) so raw
    values such as '007', ' N/A' and '$12.50' survive. Typed frames also work.
    """
    started = time.perf_counter()
    as_of = as_of or date.today()
    warnings: list[str] = []
    n_rows, n_cols = df.shape

    dup_cols = sorted({str(c) for c in df.columns[df.columns.duplicated()]})
    if dup_cols:
        warnings.append(f"duplicate column names: {', '.join(dup_cols)}")
    if n_rows == 0:
        warnings.append("table has no rows")

    try:
        exact_dups = int(df.duplicated().sum())
    except TypeError:  # unhashable cells, e.g. lists
        exact_dups = int(df.astype(str).duplicated().sum())
    if exact_dups:
        warnings.append(f"{exact_dups} exact duplicate rows ({_pct(exact_dups, n_rows)}%)")

    key_result = _key_analysis(df, list(key_cols), warnings) if key_cols else None

    columns: dict[str, dict] = {}
    for i, col in enumerate(df.columns):
        col_name = str(col) if str(col) not in columns else f"{col}__{i}"
        columns[col_name] = _profile_column(col_name, df.iloc[:, i], as_of, warnings)

    result = {
        "metadata": {
            "name": name,
            "profiled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "as_of": as_of.isoformat(),
            "key_cols": list(key_cols) if key_cols else None,
            "type_threshold_pct": TYPE_THRESHOLD * 100,
            "duration_seconds": None,
        },
        "table": {
            "row_count": n_rows,
            "column_count": n_cols,
            "exact_duplicate_row_count": exact_dups,
            "exact_duplicate_row_pct": _pct(exact_dups, n_rows),
            "key_analysis": key_result,
            "possible_key_columns": [c for c, p in columns.items() if p["is_unique"]],
            "single_value_columns": [c for c, p in columns.items() if p["is_constant"]],
            "all_null_columns": [c for c, p in columns.items() if n_rows and p["null_count"] == n_rows],
            "inferred_types": {c: p["inferred_type"] for c, p in columns.items()},
        },
        "warnings": warnings,
        "columns": columns,
    }
    result["metadata"]["duration_seconds"] = round(time.perf_counter() - started, 3)
    return _to_jsonable(result)


# --------------------------------------------------------------------------
# File loading and the command line
# --------------------------------------------------------------------------
SUPPORTED_SUFFIXES = (".csv", ".tsv", ".txt")


def _detect_encoding(path: Path) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            with open(path, encoding=enc) as f:
                while f.read(1 << 20):
                    pass
            return enc
        except UnicodeDecodeError:
            continue
    return "latin-1"  # decodes any byte


def _detect_delimiter(header_line: str, suffix: str) -> str:
    counts = {d: header_line.count(d) for d in (",", "\t", ";", "|")}
    best = max(counts, key=counts.get)
    if counts[best]:
        return best
    return "\t" if suffix == ".tsv" else ","


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_file(path: Path) -> tuple[pd.DataFrame, dict]:
    """Read a delimited file with every value as raw text (no NA conversion)."""
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ValueError(f"unsupported file type {path.suffix!r}; expected one of {SUPPORTED_SUFFIXES}")
    encoding = _detect_encoding(path)
    with open(path, encoding=encoding, newline="") as f:
        header_line = f.readline()
    delimiter = _detect_delimiter(header_line, path.suffix.lower())
    header = next(csv.reader([header_line], delimiter=delimiter), [])
    df = pd.read_csv(path, sep=delimiter, dtype=str, keep_default_na=False, na_filter=False,
                     encoding=encoding, skipinitialspace=False)
    stat = path.stat()
    source = {
        "path": str(path),
        "size_bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds"),
        "sha256": _sha256(path),
        "encoding": encoding,
        "delimiter": delimiter,
        "header": header,
        "duplicate_header_names": sorted({h for h in header if header.count(h) > 1}),
    }
    return df, source


def _parse_keys(items: list[str]) -> dict[str, list[str]]:
    keys: dict[str, list[str]] = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"--key must look like NAME=COL or NAME=COL1,COL2 (got {item!r})")
        name, cols = item.split("=", 1)
        keys[name.strip()] = [c.strip() for c in cols.split(",") if c.strip()]
    return keys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Profile delimited data files into one JSON report.")
    ap.add_argument("files", nargs="*", type=Path,
                    help="files to profile; bare names are looked up in --data-dir "
                         "(default: every .csv/.tsv/.txt file in --data-dir)")
    ap.add_argument("--data-dir", type=Path, default=Path("data/raw"),
                    help="folder holding the input files (default: data/raw)")
    ap.add_argument("--key", action="append", default=[], metavar="NAME=COL[,COL...]",
                    help="key column(s) for a table, by file name without extension; repeatable")
    ap.add_argument("--as-of", type=date.fromisoformat, default=None,
                    help="reference date YYYY-MM-DD for future-date checks (default: today)")
    ap.add_argument("--out", type=Path, default=Path("output/profiling_report.json"))
    args = ap.parse_args(argv)

    if args.files:
        # A name that doesn't exist as given (e.g. "stores.csv") is looked up in --data-dir.
        files = [p if p.exists() or not (args.data_dir / p).exists() else args.data_dir / p
                 for p in args.files]
        missing = [str(p) for p in files if not p.exists()]
        if missing:
            ap.error(f"file(s) not found: {', '.join(missing)} (also looked in {args.data_dir})")
    else:
        files = sorted(p for p in args.data_dir.glob("*") if p.suffix.lower() in SUPPORTED_SUFFIXES)
        if not files:
            ap.error(f"no input files given and no .csv/.tsv/.txt files found in {args.data_dir.resolve()}")
    keys = _parse_keys(args.key)
    unknown = set(keys) - {p.stem for p in files}
    if unknown:
        ap.error(f"--key given for unknown table(s): {', '.join(sorted(unknown))}")

    run_started = datetime.now(timezone.utc)
    t0 = time.perf_counter()
    tables: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for path in files:
        name = path.stem
        print(f"profiling {name} ({path}) ...", file=sys.stderr)
        try:
            df, source = load_file(path)
            result = profile(df, name, key_cols=keys.get(name), as_of=args.as_of)
            result["metadata"]["source"] = source
            if source["duplicate_header_names"]:
                result["warnings"].insert(0, "duplicate column names in file header (pandas renamed them): "
                                          + ", ".join(source["duplicate_header_names"]))
            tables[name] = result
        except Exception as exc:  # keep going with the other files
            errors[name] = f"{type(exc).__name__}: {exc}"
            print(f"  failed: {errors[name]}", file=sys.stderr)

    report = {
        "metadata": {
            "run_id": str(uuid.uuid4()),
            "started_at": run_started.isoformat(timespec="seconds"),
            "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "duration_seconds": round(time.perf_counter() - t0, 3),
            "profiler_version": PROFILER_VERSION,
            "python_version": platform.python_version(),
            "pandas_version": pd.__version__,
            "as_of": (args.as_of or date.today()).isoformat(),
            "settings": {
                "type_threshold_pct": TYPE_THRESHOLD * 100,
                "placeholder_nulls": sorted(PLACEHOLDER_NULLS),
                "currency_symbols": CURRENCY_SYMBOLS,
                "currency_codes": list(CURRENCY_CODES),
            },
            "inputs": [str(p) for p in files],
            "errors": errors,
        },
        "summary": {
            name: {
                "rows": t["table"]["row_count"],
                "columns": t["table"]["column_count"],
                "warning_count": len(t["warnings"]),
                "inferred_types": t["table"]["inferred_types"],
            }
            for name, t in tables.items()
        },
        "tables": tables,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(_to_jsonable(report), f, indent=2, ensure_ascii=False, allow_nan=False)
    print(f"wrote {args.out}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())





# df_store = pd.read_csv('../data/raw/stores.csv')  
# df_transactions = pd.read_csv('../data/raw/transactions.csv')  
# df_products = pd.read_csv('../data/raw/products.csv')  

# result = profile(df, "transactions", key_cols=["transaction_id"])
# result = profile(df, "stores", key_cols=["store_id"])
# result = profile(df, "products", key_cols=["product_id"])