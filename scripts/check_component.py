#!/usr/bin/env python3
"""Validate a CI/CD component template before it is published.

Runs in this repository's pipeline (`.gitlab-ci.yml`, `lint-component`) and can be
run locally:

    python scripts/check_component.py templates/ownership-notify/template.yml

Checks, in order of how much pain they save:
  * the file is valid YAML (``yaml.safe_load_all`` sees exactly one document
    after the ``---`` separator that starts the job definitions);
  * every ``$[[ inputs.x ]]`` reference is declared under ``spec.inputs``
    (a typo there fails at *include* time in every consumer's pipeline);
  * both jobs are non-blocking (``allow_failure: true``) — the whole feature is
    built on "never break a build" (§11);
  * neither job is missing a ``script``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

INPUT_REFERENCE = re.compile(r"\$\{\{\s*inputs\.([A-Za-z0-9_\-]+)\s*\}\}")

#: Jobs this component is expected to publish, and the reason each must not block.
REQUIRED_JOBS = ("ownership-mr-check", "ownership-merge-audit")


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


def check(text: str) -> list[str]:
    try:
        documents = [doc for doc in yaml.safe_load_all(text) if doc]
    except yaml.YAMLError as exc:
        return [f"invalid YAML: {exc}"]

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
    paths = [Path(arg) for arg in argv[1:]] or [Path("templates/ownership-notify/template.yml")]

    failed = False
    for path in paths:
        problems = check(path.read_text(encoding="utf-8"))
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
