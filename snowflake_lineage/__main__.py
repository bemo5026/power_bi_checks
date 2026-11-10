"""Command line entry point: read the configured models, then write the Snowflake lineage CSV files."""

import argparse
import csv
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from snowflake_lineage.auth import get_access_token
from snowflake_lineage.config import Config, load_config
from snowflake_lineage.lineage import (
    LineageRow,
    Model,
    ModelKey,
    ModelObjectRow,
    ModelTableObject,
    ObjectUsage,
    Report,
    build_lineage_rows,
    build_model_rows,
    extract_model_objects,
    summarize_objects,
)
from snowflake_lineage.powerbi_api import (
    create_session,
    get_model_definition,
    list_reports,
    list_semantic_models,
    list_workspaces,
)

UNAUTHORIZED = 401


@dataclass(frozen=True)
class SkippedModel:
    """A semantic model whose definition couldn't be read."""

    dataset_workspace: str
    dataset: str
    dataset_id: str
    report_count: int
    reason: str


def parse_arguments() -> argparse.Namespace:
    """Return the command line arguments."""
    parser = argparse.ArgumentParser(
        prog="python -m snowflake_lineage",
        description="List the Snowflake tables and views used by Power BI semantic models and reports.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.toml"),
        help="TOML file listing the semantic models and workspaces to read (default: config.toml).",
    )
    return parser.parse_args()


def _matches(wanted: set[str], *values: str) -> bool:
    """Return whether any of the values is in `wanted`, ignoring case."""
    return any(value.casefold() in wanted for value in values)


def select_workspaces(workspaces: list[dict[str, Any]], names_or_ids: list[str]) -> list[dict[str, Any]]:
    """Return the workspaces matching the configured names or IDs, or all of them when none is configured.

    Raises:
        SystemExit: If a configured workspace matches no accessible workspace.
    """
    if not names_or_ids:
        return workspaces
    selected = []
    for name_or_id in names_or_ids:
        wanted = {name_or_id.casefold()}
        matching = [ws for ws in workspaces if _matches(wanted, ws["id"], ws["displayName"])]
        if not matching:
            message = f"No accessible workspace matches '{name_or_id}'"
            raise SystemExit(message)
        selected.extend(ws for ws in matching if ws not in selected)
    return selected


def select_models(
    models: dict[ModelKey, Model],
    workspace_ids: set[str],
    names_or_ids: list[str],
) -> set[ModelKey]:
    """Return the models of the given workspaces that match the configured names or IDs.

    A name shared by models in several workspaces selects all of them.

    Raises:
        SystemExit: If a configured model matches no accessible model.
    """
    selected: set[ModelKey] = set()
    for name_or_id in names_or_ids:
        wanted = {name_or_id.casefold()}
        matching = {
            key for key, model in models.items() if key[0] in workspace_ids and _matches(wanted, key[1], model.name)
        }
        if not matching:
            message = f"No accessible semantic model matches '{name_or_id}' in the selected workspaces"
            raise SystemExit(message)
        selected |= matching
    return selected


def collect_reports_and_models(
    session: requests.Session,
    workspaces: list[dict[str, Any]],
) -> tuple[list[Report], dict[ModelKey, Model]]:
    """Return the reports and the semantic models of all the given workspaces.

    Reports are always collected everywhere, because a model can be used by
    reports in other workspaces. Paginated reports, which have no semantic
    model, are left out.
    """
    models = {
        (workspace["id"], model["id"]): Model(workspace["displayName"], model["displayName"])
        for workspace in workspaces
        for model in list_semantic_models(session, workspace["id"])
    }
    reports = [
        Report(
            workspace=workspace["displayName"],
            name=report["name"],
            id=report["id"],
            model=(report.get("datasetWorkspaceId") or workspace["id"], report["datasetId"]),
        )
        for workspace in workspaces
        for report in list_reports(session, workspace)
        if report.get("datasetId")
    ]
    return reports, models


def download_definitions(
    session: requests.Session,
    model_keys: list[ModelKey],
    parallel: int,
) -> tuple[dict[ModelKey, dict[str, Any]], dict[ModelKey, str]]:
    """Download model definitions in parallel and return them with the failures by model.

    A failure on one model (no edit access, unsupported model type) is
    recorded and the others continue. An expired token stops the run.
    """
    definitions: dict[ModelKey, dict[str, Any]] = {}
    failures: dict[ModelKey, str] = {}
    with ThreadPoolExecutor(max_workers=parallel) as executor:
        futures = {executor.submit(get_model_definition, session, *key): key for key in model_keys}
        for done, future in enumerate(as_completed(futures), start=1):
            key = futures[future]
            try:
                definitions[key] = future.result()
            except (requests.HTTPError, RuntimeError, TimeoutError) as error:
                response = getattr(error, "response", None)
                if response is not None and response.status_code == UNAUTHORIZED:
                    executor.shutdown(cancel_futures=True)
                    raise
                failures[key] = " ".join(str(error).split())[:500]
            if done % 25 == 0 or done == len(futures):
                print(f"  {done}/{len(futures)} model definitions read", file=sys.stderr)
    return definitions, failures


def write_csv(path: Path, rows: list[Any], row_type: type) -> None:
    """Write dataclass rows to a CSV file that Excel opens with the right encoding."""
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=[field.name for field in fields(row_type)])
        writer.writeheader()
        writer.writerows(asdict(row) for row in rows)


def write_outputs(
    config: Config,
    models: dict[ModelKey, Model],
    reports: list[Report],
    definitions: dict[ModelKey, dict[str, Any]],
    failures: dict[ModelKey, str],
) -> None:
    """Write the per-model and per-report results, the object summary, the skipped models and the raw definitions."""
    model_objects: dict[ModelKey, list[ModelTableObject]] = {
        key: extract_model_objects(definition) for key, definition in definitions.items()
    }
    model_rows = build_model_rows(models, model_objects, reports)
    report_rows = build_lineage_rows(reports, models, model_objects)
    usages = summarize_objects(report_rows)
    reports_per_model = Counter(report.model for report in reports)
    skipped = [
        SkippedModel(
            dataset_workspace=models[key].workspace,
            dataset=models[key].name,
            dataset_id=key[1],
            report_count=reports_per_model[key],
            reason=reason,
        )
        for key, reason in failures.items()
    ]

    output_dir = config.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    raw = {f"{key[0]}/{key[1]}": definition for key, definition in definitions.items()}
    (output_dir / "model_definitions.json").write_text(json.dumps(raw, indent=1), encoding="utf-8")
    write_csv(output_dir / "model_snowflake_objects.csv", model_rows, ModelObjectRow)
    write_csv(output_dir / "report_snowflake_lineage.csv", report_rows, LineageRow)
    write_csv(output_dir / "snowflake_objects.csv", usages, ObjectUsage)
    write_csv(output_dir / "skipped_models.csv", skipped, SkippedModel)

    print(
        f"{len(definitions)} models read: {len({row.dataset_id for row in model_rows})} use Snowflake, "
        f"{len({(row.database, row.schema, row.object_name) for row in model_rows})} distinct Snowflake objects, "
        f"used by {len({row.report_id for row in report_rows})} reports. "
        f"{len(skipped)} models could not be read (see skipped_models.csv). "
        f"Results written to {output_dir.resolve()}",
        file=sys.stderr,
    )


def main() -> None:
    """Read the configured models and the reports using them, then write the results."""
    config = load_config(parse_arguments().config)
    load_dotenv(override=True)
    session = create_session(get_access_token(), pool_size=config.parallel)

    workspaces = list_workspaces(session)
    workspace_ids = {workspace["id"] for workspace in select_workspaces(workspaces, config.workspaces)}
    print(f"Listing the reports and models of {len(workspaces)} workspaces...", file=sys.stderr)
    reports, models = collect_reports_and_models(session, workspaces)

    if config.semantic_models:
        targets = select_models(models, workspace_ids, config.semantic_models)
    else:
        targets = {key for key in models if key[0] in workspace_ids}
    reports = [report for report in reports if report.model in targets]
    print(f"Reading {len(targets)} semantic models, used by {len(reports)} reports...", file=sys.stderr)

    definitions, failures = download_definitions(session, list(targets), config.parallel)
    write_outputs(config, models, reports, definitions, failures)


if __name__ == "__main__":
    main()
