"""Stable identities for the optional ``module_overlay`` (§7.1).

Maven coordinate (``groupId:artifactId``) or npm package name, resolved from the
nearest ancestor ``pom.xml`` / ``package.json``. Used *in addition to* CODEOWNERS,
only for components that genuinely move between directories or repositories.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_PARENT_BLOCK = re.compile(r"<parent>.*?</parent>", re.DOTALL)
_IGNORED_BLOCKS = re.compile(
    r"<(dependencies|dependencyManagement|build|profiles|reporting|plugins)>.*?</\1>",
    re.DOTALL,
)
_ARTIFACT_ID = re.compile(r"<artifactId>\s*([^<\s]+)\s*</artifactId>")
_GROUP_ID = re.compile(r"<groupId>\s*([^<\s]+)\s*</groupId>")


def pom_identity(text: str) -> str | None:
    """``groupId:artifactId`` from a ``pom.xml`` (parent's groupId if not declared)."""
    parent_match = _PARENT_BLOCK.search(text)
    parent_block = parent_match.group(0) if parent_match else ""

    body = _PARENT_BLOCK.sub("", text, count=1)
    body = _IGNORED_BLOCKS.sub("", body)

    artifact = _ARTIFACT_ID.search(body)
    if not artifact:
        return None
    group = _GROUP_ID.search(body) or _GROUP_ID.search(parent_block)
    if not group:
        return None
    return f"{group.group(1)}:{artifact.group(1)}"


def package_identity(text: str) -> str | None:
    """npm package ``name`` from a ``package.json``."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    name = data.get("name")
    return name if isinstance(name, str) and name else None


def _identity_in(directory: Path) -> str | None:
    """Identity declared by an identity file directly in ``directory``, if any."""
    pom = directory / "pom.xml"
    if pom.is_file():
        identity = pom_identity(pom.read_text(encoding="utf-8", errors="replace"))
        if identity:
            return identity

    package = directory / "package.json"
    if package.is_file():
        return package_identity(package.read_text(encoding="utf-8", errors="replace"))

    return None


def find_identity(repo_root: Path, rel_path: str) -> str | None:
    """Walk up from ``rel_path`` to the nearest identity file.

    Works for deleted files too: the directory does not have to exist, we only look
    for identity files on the way up to ``repo_root``.
    """
    repo_root = repo_root.resolve()
    relative = Path(rel_path.lstrip("/"))
    if relative.is_absolute() or ".." in relative.parts:
        return None

    current = (repo_root / relative).parent
    while True:
        identity = _identity_in(current)
        if identity:
            return identity
        if current == repo_root or current.parent == current:
            return None
        current = current.parent


def identity_resolver(repo_root: Path):
    """Return a callable ``path -> identity`` for :func:`resolve_ownership`."""

    def resolve(rel_path: str) -> str | None:
        return find_identity(repo_root, rel_path)

    return resolve
