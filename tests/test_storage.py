"""Tests for the Excel workbook and the on-disk cache of model definitions."""

from dataclasses import dataclass
from pathlib import Path

from openpyxl import load_workbook

from snowflake_lineage.definition_cache import load_definitions, save_definition
from snowflake_lineage.excel import EXCEL_CELL_LIMIT, TRUNCATION_MARK, excel_value, write_workbook


@dataclass(frozen=True)
class Row:
    """A sample result row."""

    name: str
    count: int
    sql: str | None


def test_excel_value_cleans_text() -> None:
    """Control characters are removed and text longer than a cell is cut with a mark."""
    assert excel_value("a\x00b\x1fc") == "abc"
    assert excel_value(3) == 3  # noqa: PLR2004
    assert excel_value(None) is None
    long_text = excel_value("x" * (EXCEL_CELL_LIMIT + 10))
    assert isinstance(long_text, str)
    assert len(long_text) == EXCEL_CELL_LIMIT
    assert long_text.endswith(TRUNCATION_MARK)


def test_workbook_has_a_sheet_per_kind_of_row(tmp_path: Path) -> None:
    """Each sheet starts with the column names, followed by one line per row."""
    path = tmp_path / "result.xlsx"

    written = write_workbook(path, {"Rows": (Row, [Row("a", 1, None), Row("b", 2, "select 1")])}, final=True)

    assert written == path
    sheet = load_workbook(path)["Rows"]
    assert [[cell.value for cell in row] for row in sheet.iter_rows()] == [
        ["name", "count", "sql"],
        ["a", 1, None],
        ["b", 2, "select 1"],
    ]


def test_definitions_round_trip(tmp_path: Path) -> None:
    """A saved definition is loaded back; models never saved are absent."""
    saved, missing = ("w1", "m1"), ("w1", "m2")
    save_definition(tmp_path, saved, {"tables": [{"name": "Orders"}]})

    assert load_definitions(tmp_path, {saved, missing}) == {saved: {"tables": [{"name": "Orders"}]}}
