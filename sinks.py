"""Output sink abstraction: every place that writes results (extraction inserts,
resolution updates, line-resolution updates) goes through here instead of
calling db.execute directly -- so the database/CSV/Excel toggle in config.py
is the only thing that needs to change to switch modes, nothing calling it
needs to know or care which one is active.
"""
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional
import pandas as pd

import db
from config import settings


class OutputSink(ABC):
    @abstractmethod
    def insert(self, table: str, records: list[dict]) -> None:
        """Add new rows."""

    @abstractmethod
    def update(self, table: str, records: list[dict], key_column: str) -> None:
        """Update existing rows, matched on key_column."""


class DatabaseSink(OutputSink):
    """Writes directly to the Retool DB (the only writable connection)."""

    def insert(self, table: str, records: list[dict]) -> None:
        if not records:
            return
        columns = list(records[0].keys())
        col_list = ", ".join(columns)
        placeholders = ", ".join(f"%({c})s" for c in columns)
        query = f"insert into {table} ({col_list}) values ({placeholders})"
        for record in records:
            db.execute(query, record, pool_name="retool")

    def update(self, table: str, records: list[dict], key_column: str) -> None:
        if not records:
            return
        columns = [c for c in records[0].keys() if c != key_column]
        set_clause = ", ".join(f"{c} = %({c})s" for c in columns)
        query = f"update {table} set {set_clause} where {key_column} = %({key_column})s"
        for record in records:
            db.execute(query, record, pool_name="retool")


class FileExportSink(OutputSink):
    """Writes each named batch to its own CSV file, or as a sheet in one
    combined Excel workbook -- for review before a manual import, or when
    running somewhere with no DB write access at all."""

    def __init__(self, output_dir: Optional[str] = None, file_format: Optional[str] = None):
        self.output_dir = Path(output_dir or settings.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.file_format = file_format or settings.output_mode  # 'csv' or 'excel'
        self._sheets: dict[str, pd.DataFrame] = {}
        self._excel_path = self.output_dir / "output.xlsx"

    def insert(self, table: str, records: list[dict]) -> None:
        self._emit(table, records)

    def update(self, table: str, records: list[dict], key_column: str) -> None:
        self._emit(f"{table}_update", records)

    def _emit(self, name: str, records: list[dict]) -> None:
        if not records:
            return
        df = pd.DataFrame(records)
        if self.file_format == "csv":
            df.to_csv(self.output_dir / f"{name}.csv", index=False)
        elif self.file_format == "excel":
            self._sheets[name] = df
            self._write_excel()
        else:
            raise ValueError(f"FileExportSink doesn't support format '{self.file_format}'")

    def _write_excel(self) -> None:
        # Rewrites the whole workbook each call -- simpler than appending to an
        # already-saved xlsx, and batches here are small enough that this is fine.
        with pd.ExcelWriter(self._excel_path, engine="openpyxl") as writer:
            for name, df in self._sheets.items():
                df.to_excel(writer, sheet_name=name[:31], index=False)  # Excel sheet names cap at 31 chars


def get_output_sink() -> OutputSink:
    if settings.output_mode == "database":
        return DatabaseSink()
    return FileExportSink()