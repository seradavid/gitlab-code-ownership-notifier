#!/usr/bin/env python3
"""Validate the CI configuration before it is published.

Runs in both pipelines (`.github/workflows/ci.yml`, `.gitlab-ci.yml`) and locally:

    python scripts/check_component.py                 # every CI file in the repo
    python scripts/check_component.py .gitlab-ci.yml   # just one

Checks, in order of how much pain they save:
  * every shell step (``script:``, ``before_script:``, ``after_script:``, ``run:``) is a
    string or a nested list of strings. An unquoted YAML scalar containing ``": "``
    parses as a mapping instead, and GitLab then rejects the entire pipeline with
    "script config should be a string or a nested array of strings";
  * the file is valid YAML;
  * for the component template: every ``$[[ inputs.x ]]`` reference is declared under
    ``spec.inputs`` (a typo there fails at *include* time in every consumer's pipeline);
  * both component jobs are non-blocking (``allow_failure: true``) — the whole feature
    is built on "never break a build" (docs/design.md §11);
  * neither component job is missing a ``script``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

INPUT_REFERENCE = re.compile(r"\$\{\{\s*inputs\.([A-Za-z0-9_\-]+)\s*\}\}")

#: Jobs this component is expected to publish, and the reason each must not block.
REQUIRED_JOBS = ("ownership-mr-check", "ownership-merge-audit")

#: Keys whose value is a shell command (GitLab) or a shell block (GitHub Actions).
SHELL_KEYS = ("script", "before_script", "after_script", "run")

#: The CI files this repository ships, checked when no arguments are given.
DEFAULT_TARGETS = (
    "templates/ownership-notify/template.yml",
    ".gitlab-ci.yml",
    ".github/workflows/ci.yml",
    ".github/workflows/release.yml",
)


def _load(text: str) -> list:
    """Parse every YAML document; raises ``yaml.YAMLError`` on invalid syntax."""
    return [document for document in yaml.safe_load_all(text) if document]


def _check_shell_value(location: str, value) -> list[str]:  # noqa: ANN001
    """A shell step must be a string, or a (nested) list of strings."""
    if isinstance(value, str):
        return []
    if isinstance(value, list):
        problems: list[str] = []
        for index, item in enumerate(value):
            problems.extend(_check_shell_value(f"{location}[{index}]", item))
        return problems
    return [
        f"{location} is a {type(value).__name__}, not a string: an unquoted scalar "
        "containing ': ' parses as a mapping — quote the whole line",
    ]


def _walk_shell_steps(node, location: str, problems: list[str]) -> None:  # noqa: ANN001
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{location}.{key}" if location else str(key)
            if key in SHELL_KEYS:
                problems.extend(_check_shell_value(here, value))
            else:
                _walk_shell_steps(value, here, problems)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _walk_shell_steps(item, f"{location}[{index}]", problems)


def check_pipeline(text: str) -> list[str]:
    """Validate the shell steps of any GitLab or GitHub CI file."""
    try:
        documents = _load(text)
    except yaml.YAMLError as exc:
        return [f"invalid YAML: {exc}"]

    problems: list[str] = []
    for document in documents:
        _walk_shell_steps(document, "", problems)
    return problems


def _check_inputs(header: dict) -> tuple[list[str], set[str]]:
    """Every input needs a description and a default (or an explicit type)."""
    inputs = (header.get("spec") or {}).get("inputs") or {}
    problems: list[str] = []

    if not inputs:
        problems.append("spec.inputs is empty; consumers cannot configure the component")

    for name, spec in inputs.items():
        if not isinstance(spec, dict):
            problems.append(f"input '{name}' must be a mapping")
            continue
        if "description" not in spec:
            problems.append(f"input '{name}' has no description")
        if spec.get("default", f"$[[ inputs.{name} ]]") is None:
            problems.append(f"input '{name}' has no default and no required marker")

    return problems, set(inputs)


def _check_references(text: str, declared: set[str]) -> list[str]:
    return [
        f"undeclared input referenced: '$[[ inputs.{name} ]]'"
        for name in INPUT_REFERENCE.findall(text)
        if name not in declared
    ]


def _check_job(name: str, body: object) -> list[str]:
    """A published job must run something, never block, and be opt-out-able."""
    if not isinstance(body, dict):
        return [f"missing job '{name}'"]

    problems: list[str] = []
    if not body.get("script"):
        problems.append(f"job '{name}' has no script")
    if body.get("allow_failure") is not True:
        problems.append(f"job '{name}' must set allow_failure: true")

    rules = body.get("rules")
    if not rules:
        problems.append(f"job '{name}' has no rules")
    elif not any(rule.get("when") == "never" for rule in rules):
        problems.append(f"job '{name}' has no opt-out rule")
    return problems


def check_component(text: str) -> list[str]:
    """Validate a CI/CD component template; a file without ``spec:`` is not one."""
    try:
        documents = _load(text)
    except yaml.YAMLError as exc:
        return [f"invalid YAML: {exc}"]

    if not documents or not isinstance(documents[0], dict) or "spec" not in documents[0]:
        return []  # a pipeline, not a component: check_pipeline covers it

    if len(documents) != 2:
        return [f"expected 2 YAML documents (header + jobs), found {len(documents)}"]

    header, jobs = documents
    problems, declared = _check_inputs(header)
    problems.extend(_check_references(text, declared))

    for name in REQUIRED_JOBS:
        problems.extend(_check_job(name, jobs.get(name)))

    problems.extend(
        f"job '{name}' is not a mapping"
        for name, body in jobs.items()
        if not name.startswith(".") and not isinstance(body, dict)
    )

    return problems


def main(argv: list[str]) -> int:
    paths = [Path(arg) for arg in argv[1:]] or [Path(target) for target in DEFAULT_TARGETS]

    failed = False
    for path in paths:
        if not path.is_file():
            failed = True
            print(f"{path}: missing")
            continue

        text = path.read_text(encoding="utf-8")
        problems = check_pipeline(text) + check_component(text)
        if problems:
            failed = True
            print(f"{path}: {len(problems)} problem(s)")
            for problem in problems:
                print(f"  - {problem}")
        else:
            print(f"{path}: ok")
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover - script entry point
    raise SystemExit(main(sys.argv))
