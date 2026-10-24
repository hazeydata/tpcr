"""Atomic file writers for competition outputs (tmp file + os.replace)."""

from __future__ import annotations

import json
import os
from pathlib import Path


def atomic_write_text(path: Path | str, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def atomic_write_json(path: Path | str, obj) -> None:
    atomic_write_text(path, json.dumps(obj, indent=2, default=str))


def atomic_write_parquet(df, path: Path | str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def atomic_write_csv(df, path: Path | str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    df.to_csv(tmp, index=False)
    os.replace(tmp, path)
