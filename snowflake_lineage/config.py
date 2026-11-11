"""Read the run configuration from a TOML file."""

import tomllib
from dataclasses import dataclass
from pathlib import Path

KNOWN_KEYS = {"workspaces", "semantic_models", "output_dir", "parallel", "batch_size", "reuse_downloaded"}


@dataclass(frozen=True)
class Config:
    """What to read and where to write it.

    Attributes:
        workspaces: Workspace names or IDs to limit the run to. Empty means every accessible workspace.
        semantic_models: Semantic model names or IDs to read. Empty means every model used by a report.
        output_dir: Folder for the result files.
        parallel: Number of model definitions downloaded at the same time.
        batch_size: The Excel workbook is rewritten every time this many models are downloaded.
        reuse_downloaded: Reuse the model definitions saved by earlier runs instead of downloading them again.
    """

    workspaces: list[str]
    semantic_models: list[str]
    output_dir: Path
    parallel: int
    batch_size: int
    reuse_downloaded: bool


def _string_list(data: dict[str, object], key: str) -> list[str]:
    """Return a list of strings from the config, failing on any other type."""
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        message = f"'{key}' in the config must be a list of strings"
        raise TypeError(message)
    return [item.strip() for item in value if item.strip()]


def load_config(path: Path) -> Config:
    """Return the configuration stored in a TOML file.

    Raises:
        ValueError: If the file has keys this tool doesn't know (usually a typo).
    """
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    unknown = data.keys() - KNOWN_KEYS
    if unknown:
        message = f"Unknown keys in {path}: {sorted(unknown)}. Allowed keys: {sorted(KNOWN_KEYS)}"
        raise ValueError(message)
    return Config(
        workspaces=_string_list(data, "workspaces"),
        semantic_models=_string_list(data, "semantic_models"),
        output_dir=Path(data.get("output_dir", "output")),
        parallel=int(data.get("parallel", 8)),
        batch_size=int(data.get("batch_size", 25)),
        reuse_downloaded=bool(data.get("reuse_downloaded", True)),
    )
