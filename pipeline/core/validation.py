"""Data validation primitives.

Used by every step to verify inputs and outputs.
Fail loud — no silent plausibility errors.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd


class ValidationError(Exception):
    """Raised when data validation fails. Pipeline should stop."""
    pass


def require_file(path: Path, description: str = "") -> Path:
    """Assert a file exists. Raise ValidationError if not."""
    if not path.exists():
        raise ValidationError(f"Required file missing: {path} ({description})")
    if path.stat().st_size == 0:
        raise ValidationError(f"Required file is empty: {path} ({description})")
    return path


def require_parquet_rows(path: Path, min_rows: int = 1, description: str = "") -> int:
    """Assert a parquet file has at least min_rows. Return actual count."""
    require_file(path, description)
    try:
        import pyarrow.parquet as pq
        n = pq.read_metadata(str(path)).num_rows
    except ImportError:
        # Fallback: read with pandas (slower but works)
        n = len(pd.read_parquet(path, columns=[]))
    if n < min_rows:
        raise ValidationError(
            f"Parquet file has {n} rows, expected >= {min_rows}: {path} ({description})"
        )
    return n


def require_columns(df: pd.DataFrame, columns: list[str], context: str = ""):
    """Assert a DataFrame has required columns."""
    missing = set(columns) - set(df.columns)
    if missing:
        raise ValidationError(
            f"Missing columns {missing} in {context}. Have: {list(df.columns)}"
        )


def require_no_nulls(df: pd.DataFrame, columns: list[str], context: str = ""):
    """Assert no nulls in specified columns."""
    for col in columns:
        n_null = df[col].isna().sum()
        if n_null > 0:
            raise ValidationError(
                f"Column '{col}' has {n_null} nulls in {context}"
            )


def require_range(value: float, min_val: float, max_val: float, name: str):
    """Assert a value is within expected range."""
    if not (min_val <= value <= max_val):
        raise ValidationError(
            f"{name}={value} outside expected range [{min_val}, {max_val}]"
        )


class StageFailed(ValidationError):
    """Raised after a stage finishes its work when real errors occurred (stage exits nonzero)."""
    pass
