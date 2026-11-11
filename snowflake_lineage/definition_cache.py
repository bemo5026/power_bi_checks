"""Keep each downloaded model definition on disk so an interrupted run can resume."""

import json
from pathlib import Path
from typing import Any

from snowflake_lineage.lineage import ModelKey


def _cache_file(cache_dir: Path, key: ModelKey) -> Path:
    """Return the file holding a model's definition."""
    workspace_id, model_id = key
    return cache_dir / f"{workspace_id}_{model_id}.json"


def save_definition(cache_dir: Path, key: ModelKey, definition: dict[str, Any]) -> None:
    """Store a model definition, through a temporary file so a crash never leaves half a file."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_file(cache_dir, key)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(definition), encoding="utf-8")
    temporary.replace(path)


def load_definitions(cache_dir: Path, keys: set[ModelKey]) -> dict[ModelKey, dict[str, Any]]:
    """Return the stored definitions of the given models; models never downloaded are absent."""
    definitions = {}
    for key in keys:
        path = _cache_file(cache_dir, key)
        if path.exists():
            definitions[key] = json.loads(path.read_text(encoding="utf-8"))
    return definitions
