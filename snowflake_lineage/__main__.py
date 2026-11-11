"""Command line entry point: read the configured models, then write the Snowflake lineage workbook."""

import argparse
import sys
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from snowflake_lineage.auth import get_access_token
from snowflake_lineage.config import Config, load_config
from snowflake_lineage.definition_cache import load_definitions, save_definition
from snowflake_lineage.excel import write_workbook
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
    TRANSIENT_ERRORS,
    create_session,
    get_model_definition,
    list_reports,
    list_semantic_models,
    list_workspaces,
)

UNAUTHORIZED = 401
WORKBOOK_NAME = "snowflake_lineage.xlsx"
DEFINITIONS_DIR = "model_definitions"


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
) -> Iterator[tuple[ModelKey, dict[str, Any] | None, str | None]]:
    """Download model definitions in parallel, yielding (model, definition, None) or (model, None, error).

    A failure on one model (no edit access, unsupported model type, network
    errors that outlast the retries) is yielded and the others continue. An
    expired token stops the run.
    """
    with ThreadPoolExecutor(max_workers=parallel) as executor:
        futures = {executor.submit(get_model_definition, session, *key): key for key in model_keys}
        for future in as_completed(futures):
            key = futures[future]
            try:
                definition = future.result()
            except (requests.HTTPError, RuntimeError, TimeoutError, *TRANSIENT_ERRORS) as error:
                response = getattr(error, "response", None)
                if response is not None and response.status_code == UNAUTHORIZED:
                    executor.shutdown(cancel_futures=True)
                    raise
                yield key, None, " ".join(str(error).split())[:500]
            else:
                yield key, definition, None


def write_outputs(  # noqa: PLR0913
    config: Config,
    models: dict[ModelKey, Model],
    reports: list[Report],
    definitions: dict[ModelKey, dict[str, Any]],
    failures: dict[ModelKey, str],
    *,
    final: bool,
) -> None:
    """Rewrite the Excel workbook with the models read so far: per model, per report, per object, skipped."""
    model_objects: dict[ModelKey, list[ModelTableObject]] = {
        key: extract_model_objects(definition) for key, definition in definitions.items()
    }
    model_rows = build_model_rows(models, model_objects, reports)
    report_rows = build_lineage_rows(reports, models, model_objects)
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
    sheets = {
        "Model objects": (ModelObjectRow, model_rows),
        "Report lineage": (LineageRow, report_rows),
        "Snowflake objects": (ObjectUsage, summarize_objects(report_rows)),
        "Skipped models": (SkippedModel, skipped),
    }
    written = write_workbook(config.output_dir / WORKBOOK_NAME, sheets, final=final)
    if written is None:
        print(f"  {WORKBOOK_NAME} is open in Excel: this save is skipped.", file=sys.stderr)
    elif final:
        print(
            f"{len(definitions)} models read: {len({row.dataset_id for row in model_rows})} use Snowflake, "
            f"{len({(row.database, row.schema, row.object_name) for row in model_rows})} distinct Snowflake "
            f"objects, used by {len({row.report_id for row in report_rows})} reports. "
            f"{len(skipped)} models could not be read (see the 'Skipped models' sheet). "
            f"Results written to {written.resolve()}",
            file=sys.stderr,
        )


def main() -> None:
    """Read the configured models and the reports using them, saving the results every batch."""
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

    cache_dir = config.output_dir / DEFINITIONS_DIR
    definitions = load_definitions(cache_dir, targets) if config.reuse_downloaded else {}
    pending = [key for key in targets if key not in definitions]
    print(
        f"{len(targets)} semantic models used by {len(reports)} reports: {len(definitions)} already downloaded "
        f"in {cache_dir}, {len(pending)} to download.",
        file=sys.stderr,
    )

    config.output_dir.mkdir(parents=True, exist_ok=True)
    failures: dict[ModelKey, str] = {}
    downloads = download_definitions(session, pending, config.parallel)
    for done, (key, definition, error) in enumerate(downloads, start=1):
        if definition is None:
            failures[key] = error or "unknown error"
        else:
            definitions[key] = definition
            save_definition(cache_dir, key, definition)
        if done % config.batch_size == 0:
            print(f"  {done}/{len(pending)} model definitions read, saving {WORKBOOK_NAME}", file=sys.stderr)
            write_outputs(config, models, reports, definitions, failures, final=False)
    write_outputs(config, models, reports, definitions, failures, final=True)


if __name__ == "__main__":
    main()
