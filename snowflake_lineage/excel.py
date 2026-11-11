"""Write result rows to an Excel workbook, one sheet per kind of row."""

from dataclasses import fields
from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

EXCEL_CELL_LIMIT = 32767
TRUNCATION_MARK = " [truncated, see the model definition files]"

Sheet = tuple[type, list[Any]]
"""The dataclass type of the rows and the rows themselves."""


def excel_value(value: object) -> object:
    """Return a value Excel accepts: control characters removed, text cut to the cell limit."""
    if not isinstance(value, str):
        return value
    text = ILLEGAL_CHARACTERS_RE.sub("", value)
    if len(text) > EXCEL_CELL_LIMIT:
        return text[: EXCEL_CELL_LIMIT - len(TRUNCATION_MARK)] + TRUNCATION_MARK
    return text


def _build_workbook(sheets: dict[str, Sheet]) -> Workbook:
    """Return a workbook with a filterable sheet per entry, the header row frozen."""
    workbook = Workbook(write_only=True)
    for title, (row_type, rows) in sheets.items():
        worksheet = workbook.create_sheet(title)
        columns = [field.name for field in fields(row_type)]
        worksheet.freeze_panes = "A2"
        worksheet.auto_filter.ref = f"A1:{_column_letter(len(columns))}{len(rows) + 1}"
        worksheet.append(columns)
        for row in rows:
            worksheet.append([excel_value(getattr(row, column)) for column in columns])
    return workbook


def _column_letter(number: int) -> str:
    """Return the Excel letter of a 1-based column number (1 -> A, 27 -> AA)."""
    letters = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def write_workbook(path: Path, sheets: dict[str, Sheet], *, final: bool) -> Path | None:
    """Write the workbook through a temporary file and return where it was written.

    When the workbook is open in Excel, Windows refuses to replace it. An
    intermediate save is then skipped (None is returned; the next save
    catches up), and the final save goes to a timestamped file next to it.
    """
    temporary = path.with_name(f"~{path.stem}.tmp.xlsx")
    _build_workbook(sheets).save(temporary)
    try:
        temporary.replace(path)
    except PermissionError:
        if not final:
            temporary.unlink()
            return None
        fallback = path.with_name(f"{path.stem}_{datetime.now():%Y%m%d_%H%M%S}{path.suffix}")  # noqa: DTZ005
        temporary.replace(fallback)
        return fallback
    return path
