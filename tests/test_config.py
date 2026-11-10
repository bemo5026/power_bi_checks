"""Tests for reading the run configuration."""

from pathlib import Path

import pytest

from snowflake_lineage.config import load_config


def test_config_values_and_defaults(tmp_path: Path) -> None:
    """Configured lists are read, blank entries dropped, and missing keys get defaults."""
    path = tmp_path / "config.toml"
    path.write_text('semantic_models = ["Sales model", " "]\n', encoding="utf-8")

    config = load_config(path)

    assert config.semantic_models == ["Sales model"]
    assert config.workspaces == []
    assert config.output_dir == Path("output")
    assert config.parallel == 8  # noqa: PLR2004


def test_unknown_key_fails(tmp_path: Path) -> None:
    """A misspelled key is reported rather than ignored."""
    path = tmp_path / "config.toml"
    path.write_text('semantic_model = ["Sales model"]\n', encoding="utf-8")

    with pytest.raises(ValueError, match="semantic_model"):
        load_config(path)
