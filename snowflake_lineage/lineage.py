"""Link each report to the Snowflake objects read by its semantic model."""

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from snowflake_lineage.m_parser import SnowflakeObject, extract_snowflake_objects

ModelKey = tuple[str, str]
"""A semantic model, identified by (workspace ID, model ID)."""


@dataclass(frozen=True)
class Report:
    """A report and the semantic model it is built on."""

    workspace: str
    name: str
    id: str
    model: ModelKey


@dataclass(frozen=True)
class Model:
    """A semantic model and where it lives."""

    workspace: str
    name: str


@dataclass(frozen=True)
class ModelTableObject:
    """A Snowflake object read by one table of a semantic model."""

    model_table: str
    snowflake: SnowflakeObject


@dataclass(frozen=True)
class LineageRow:
    """A Snowflake object used by a report, through one table of its semantic model."""

    workspace: str
    report: str
    report_id: str
    dataset_workspace: str
    dataset: str
    dataset_id: str
    model_table: str
    server: str | None
    warehouse: str | None
    database: str | None
    schema: str | None
    object_name: str | None
    object_kind: str
    native_sql: str | None


@dataclass(frozen=True)
class ModelObjectRow:
    """A Snowflake object read by one table of a semantic model."""

    dataset_workspace: str
    dataset: str
    dataset_id: str
    report_count: int
    report_ids: str
    reports: str
    model_table: str
    server: str | None
    warehouse: str | None
    database: str | None
    schema: str | None
    object_name: str | None
    object_kind: str
    native_sql: str | None


@dataclass(frozen=True)
class ObjectUsage:
    """A distinct Snowflake object and the reports that use it."""

    database: str | None
    schema: str | None
    object_name: str | None
    object_kind: str
    report_count: int
    report_ids: str
    reports: str


ReportLabel = tuple[str, str, str]
"""A report as (workspace, name, ID)."""


def _join_reports(reports: set[ReportLabel]) -> tuple[int, str, str]:
    """Return the number of reports, their IDs and their "workspace / name" labels, in a stable order."""
    ordered = sorted(reports)
    return (
        len(ordered),
        "; ".join(report_id for _, _, report_id in ordered),
        "; ".join(f"{workspace} / {name}" for workspace, name, _ in ordered),
    )


def _m_text(expression: str | list[str]) -> str:
    """Return an M expression as text: model.bim stores long ones as a list of lines."""
    return "\n".join(expression) if isinstance(expression, list) else expression


def extract_model_objects(definition: dict[str, Any]) -> list[ModelTableObject]:
    """Return the Snowflake objects read by each table of a model's TMSL definition.

    Only Power Query (``m``) partitions are read: calculated tables and
    Direct Lake partitions don't query Snowflake.
    """
    shared_expressions = {
        item["name"]: _m_text(item["expression"])
        for item in definition.get("expressions", [])
        if item.get("kind") == "m"
    }
    return [
        ModelTableObject(table["name"], snowflake_object)
        for table in definition.get("tables", [])
        for partition in table.get("partitions", [])
        if partition["source"]["type"] == "m"
        for snowflake_object in extract_snowflake_objects(
            _m_text(partition["source"]["expression"]),
            shared_expressions,
        )
    ]


def build_model_rows(
    models: dict[ModelKey, Model],
    model_objects: dict[ModelKey, list[ModelTableObject]],
    reports: list[Report],
) -> list[ModelObjectRow]:
    """Return one row per semantic model, model table and Snowflake object.

    Each row lists the reports built on the model; a count of 0 means no report uses it.
    """
    reports_by_model: dict[ModelKey, set[ReportLabel]] = defaultdict(set)
    for report in reports:
        reports_by_model[report.model].add((report.workspace, report.name, report.id))
    return [
        ModelObjectRow(
            models[key].workspace,
            models[key].name,
            key[1],
            *_join_reports(reports_by_model[key]),
            model_table=item.model_table,
            server=item.snowflake.server,
            warehouse=item.snowflake.warehouse,
            database=item.snowflake.database,
            schema=item.snowflake.schema,
            object_name=item.snowflake.name,
            object_kind=item.snowflake.kind,
            native_sql=item.snowflake.native_sql,
        )
        for key, items in model_objects.items()
        for item in items
    ]


def build_lineage_rows(
    reports: list[Report],
    models: dict[ModelKey, Model],
    model_objects: dict[ModelKey, list[ModelTableObject]],
) -> list[LineageRow]:
    """Return one row per report, model table and Snowflake object.

    Reports whose model definition couldn't be read (absent from
    `model_objects`) and models without Snowflake sources produce no rows.
    """
    return [
        LineageRow(
            workspace=report.workspace,
            report=report.name,
            report_id=report.id,
            dataset_workspace=models[report.model].workspace,
            dataset=models[report.model].name,
            dataset_id=report.model[1],
            model_table=item.model_table,
            server=item.snowflake.server,
            warehouse=item.snowflake.warehouse,
            database=item.snowflake.database,
            schema=item.snowflake.schema,
            object_name=item.snowflake.name,
            object_kind=item.snowflake.kind,
            native_sql=item.snowflake.native_sql,
        )
        for report in reports
        for item in model_objects.get(report.model, [])
    ]


def summarize_objects(rows: list[LineageRow]) -> list[ObjectUsage]:
    """Return each distinct Snowflake object with the reports that use it, most used first."""
    reports_by_object: dict[tuple[str | None, str | None, str | None, str], set[ReportLabel]] = defaultdict(set)
    for row in rows:
        key = (row.database, row.schema, row.object_name, row.object_kind)
        reports_by_object[key].add((row.workspace, row.report, row.report_id))
    usages = [ObjectUsage(*key, *_join_reports(reports)) for key, reports in reports_by_object.items()]
    return sorted(
        usages,
        key=lambda usage: (
            -usage.report_count,
            usage.database or "",
            usage.schema or "",
            usage.object_name or "",
        ),
    )
