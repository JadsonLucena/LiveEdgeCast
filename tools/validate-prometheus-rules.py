#!/usr/bin/env python3
"""Render PrometheusRule CRDs and validate them with promtool when available.

Prometheus Operator stores rule groups under spec.groups, while promtool expects
plain rule files with groups at the document root. This helper extracts those
rule groups into a temporary rule file before invoking promtool.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RULE_GLOBS = ("k8s/**/*.yaml", "k8s/**/*.yml")


def iter_yaml_paths() -> list[Path]:
    paths: set[Path] = set()
    for pattern in DEFAULT_RULE_GLOBS:
        paths.update(ROOT.glob(pattern))
    return sorted(paths)


def load_prometheus_rule_groups() -> tuple[list[dict], list[Path]]:
    groups: list[dict] = []
    sources: list[Path] = []
    for path in iter_yaml_paths():
        with path.open(encoding="utf-8") as handle:
            for document in yaml.safe_load_all(handle):
                if not isinstance(document, dict):
                    continue
                if document.get("kind") != "PrometheusRule":
                    continue
                spec = document.get("spec") or {}
                rule_groups = spec.get("groups") or []
                if not isinstance(rule_groups, list):
                    raise SystemExit(f"{path.relative_to(ROOT)} has PrometheusRule spec.groups that is not a list")
                groups.extend(rule_groups)
                sources.append(path)
    return groups, sources


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--require-promtool",
        action="store_true",
        help="fail instead of warning when promtool is not installed",
    )
    args = parser.parse_args()

    groups, sources = load_prometheus_rule_groups()
    if not groups:
        print("No PrometheusRule resources found")
        return 0

    promtool = shutil.which("promtool")
    if promtool is None:
        message = "promtool not found; skipped parser-level validation for " + ", ".join(
            str(path.relative_to(ROOT)) for path in sources
        )
        if args.require_promtool:
            print(f"ERROR: {message}", file=sys.stderr)
            return 1
        print(f"WARNING: {message}")
        return 0

    with tempfile.TemporaryDirectory() as tmpdir:
        rendered = Path(tmpdir) / "prometheus-rules.yaml"
        rendered.write_text(yaml.safe_dump({"groups": groups}, sort_keys=False), encoding="utf-8")
        subprocess.run([promtool, "check", "rules", str(rendered)], check=True)

    source_list = ", ".join(str(path.relative_to(ROOT)) for path in sources)
    print(f"Validated {len(groups)} Prometheus rule group(s) from {source_list}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
