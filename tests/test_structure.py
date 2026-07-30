"""Static HACS and dependency checks."""

from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).parents[1]
COMPONENT = ROOT / "custom_components" / "windmillac"


def test_manifest_and_hacs_metadata_target_supported_versions() -> None:
    manifest = json.loads((COMPONENT / "manifest.json").read_text())
    hacs = json.loads((ROOT / "hacs.json").read_text())

    assert manifest["version"] == "1.1.1"
    assert manifest["requirements"] == []
    assert hacs["homeassistant"] == "2026.7.0"


def test_blocking_legacy_clients_are_absent() -> None:
    source = "\n".join(path.read_text() for path in COMPONENT.glob("*.py"))

    assert "import requests" not in source
    assert "blynklib" not in source
    assert "setLevel(logging.DEBUG)" not in source
    assert "Request URL" not in source


def test_component_source_parses_with_python_3_13_grammar() -> None:
    for source_path in COMPONENT.glob("*.py"):
        ast.parse(source_path.read_text(), filename=str(source_path), feature_version=(3, 13))


def test_test_lock_pins_home_assistant_2026_7_4() -> None:
    lock = (ROOT / "uv.lock").read_text()

    assert 'name = "homeassistant"' in lock
    assert 'version = "2026.7.4"' in lock
