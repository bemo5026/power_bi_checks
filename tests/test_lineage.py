"""Tests for linking reports to the Snowflake objects of their semantic models."""

from snowflake_lineage.lineage import (
    Model,
    Report,
    build_lineage_rows,
    build_model_rows,
    extract_model_objects,
    summarize_objects,
)

ORDERS_EXPRESSION = [
    "let",
    '    Source = Snowflake.Databases("acme.snowflakecomputing.com", "WH"),',
    '    Db = Source{[Name="PROD", Kind="Database"]}[Data],',
    '    Sch = Db{[Name="SALES", Kind="Schema"]}[Data],',
    '    T = Sch{[Name="ORDERS", Kind="Table"]}[Data]',
    "in",
    "    T",
]

DEFINITION = {
    "tables": [
        {"name": "Orders", "partitions": [{"source": {"type": "m", "expression": ORDERS_EXPRESSION}}]},
        {"name": "Calendar", "partitions": [{"source": {"type": "calculated", "expression": "CALENDARAUTO()"}}]},
    ],
}
SALES_MODEL = ("w1", "d1")
MODELS = {SALES_MODEL: Model("Finance", "Sales model")}
REPORTS = [
    Report("Finance", "Revenue", "r1", SALES_MODEL),
    Report("Marketing", "Campaigns", "r3", SALES_MODEL),
    Report("Marketing", "Unreadable", "r4", ("w2", "d9")),
]


def test_model_objects_come_from_m_partitions() -> None:
    """Power Query partitions are parsed, stored as a list of lines; calculated tables are ignored."""
    [item] = extract_model_objects(DEFINITION)

    assert (item.model_table, item.snowflake.database, item.snowflake.name) == ("Orders", "PROD", "ORDERS")


def test_reports_share_a_model_across_workspaces() -> None:
    """Each report using the model gets a row; reports whose model wasn't read get none."""
    rows = build_lineage_rows(REPORTS, MODELS, {SALES_MODEL: extract_model_objects(DEFINITION)})

    assert [(row.workspace, row.report, row.dataset_workspace, row.object_name) for row in rows] == [
        ("Finance", "Revenue", "Finance", "ORDERS"),
        ("Marketing", "Campaigns", "Finance", "ORDERS"),
    ]


def test_model_rows_list_objects_and_reports_per_model() -> None:
    """Each model row carries the reports built on the model."""
    [row] = build_model_rows(MODELS, {SALES_MODEL: extract_model_objects(DEFINITION)}, REPORTS)

    assert (row.dataset_workspace, row.dataset, row.model_table, row.object_name) == (
        "Finance",
        "Sales model",
        "Orders",
        "ORDERS",
    )
    assert (row.report_count, row.report_ids, row.reports) == (
        2,
        "r1; r3",
        "Finance / Revenue; Marketing / Campaigns",
    )


def test_model_without_report_has_zero_reports() -> None:
    """A model no report uses still gets rows, with a report count of 0."""
    [row] = build_model_rows(MODELS, {SALES_MODEL: extract_model_objects(DEFINITION)}, [])

    assert (row.report_count, row.report_ids, row.reports) == (0, "", "")


def test_summary_counts_reports_per_object() -> None:
    """The summary lists each object once with the reports using it."""
    rows = build_lineage_rows(REPORTS, MODELS, {SALES_MODEL: extract_model_objects(DEFINITION)})
    [usage] = summarize_objects(rows)

    assert (usage.database, usage.schema, usage.object_name, usage.report_count) == ("PROD", "SALES", "ORDERS", 2)
    assert usage.report_ids == "r1; r3"
    assert usage.reports == "Finance / Revenue; Marketing / Campaigns"
