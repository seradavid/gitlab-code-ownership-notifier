"""Stable identities for the optional ``module_overlay`` (§7.1).

Maven coordinate (``groupId:artifactId``) or npm package name, resolved from the
nearest ancestor ``pom.xml`` / ``package.json``. Used *in addition to* CODEOWNERS,
only for components that genuinely move between directories or repositories.

Identity files are read through an injected reader, so the same logic serves a local
checkout and the ``GIT_STRATEGY: none`` component (which fetches them through the API).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

_PARENT_BLOCK = re.compile(r"<parent>.*?</parent>", re.DOTALL)
_IGNORED_BLOCKS = re.compile(
    r"<(dependencies|dependencyManagement|build|profiles|reporting|plugins)>.*?</\1>",
    re.DOTALL,
)
_ARTIFACT_ID = re.compile(r"<artifactId>\s*([^<\s]+)\s*</artifactId>")
_GROUP_ID = re.compile(r"<groupId>\s*([^<\s]+)\s*</groupId>")

#: Reads a repository-relative path (``""``-rooted, ``/`` separators) to its text,
#: or ``None`` when the file does not exist / cannot be read.
IdentityReader = Callable[[str], "str | None"]


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


def _identity_at(read: IdentityReader, directory: str) -> str | None:
    """Identity declared by an identity file directly in ``directory`` (root = ``""``)."""
    prefix = f"{directory}/" if directory else ""

    pom = read(f"{prefix}pom.xml")
    if pom is not None:
        identity = pom_identity(pom)
        if identity:
            return identity

    package = read(f"{prefix}package.json")
    if package is not None:
        return package_identity(package)

    return None


def _ancestor_dirs(rel_path: str) -> list[str]:
    """Directories to search, nearest first, the repository root as ``""``."""
    relative = Path(rel_path.lstrip("/"))
    if relative.is_absolute() or ".." in relative.parts:
        return []

    directories: list[str] = []
    current = relative.parent
    while True:
        root = str(current) in ("", ".")
        directories.append("" if root else current.as_posix())
        if root:
            return directories
        current = current.parent


def find_identity_with(read: IdentityReader, rel_path: str) -> str | None:
    """Walk up from ``rel_path`` to the nearest identity file, through ``read``.

    Works for deleted files too: the directory does not have to exist, only an
    ancestor identity file has to be readable through ``read``.
    """
    for directory in _ancestor_dirs(rel_path):
        identity = _identity_at(read, directory)
        if identity:
            return identity
    return None


def _filesystem_reader(repo_root: Path) -> IdentityReader:
    root = repo_root.resolve()

    def read(path: str) -> str | None:
        candidate = root / path
        if not candidate.is_file():
            return None
        return candidate.read_text(encoding="utf-8", errors="replace")

    return read


def _caching(reader: IdentityReader) -> IdentityReader:
    """Cache per path; a failed read is a miss, never an error (§11)."""
    cache: dict[str, str | None] = {}

    def read(path: str) -> str | None:
        if path not in cache:
            try:
                cache[path] = reader(path)
            except Exception:  # noqa: BLE001 - identity resolution must not break a run
                cache[path] = None
        return cache[path]

    return read


def find_identity(repo_root: Path, rel_path: str) -> str | None:
    """Filesystem-only lookup (developer machines, jobs that do check out)."""
    return find_identity_with(_filesystem_reader(repo_root), rel_path)


def identity_resolver(
    repo_root: Path | None = None,
    *,
    remote: IdentityReader | None = None,
) -> Callable[[str], str | None] | None:
    """Return a ``path -> identity`` callable, or ``None`` when neither source exists.

    A local checkout is tried first and ``remote`` (the GitLab API) second, so the
    same resolver works with a checkout, without one, and offline.
    """
    readers: list[IdentityReader] = []
    if repo_root is not None:
        readers.append(_filesystem_reader(repo_root))
    if remote is not None:
        readers.append(_caching(remote))
    if not readers:
        return None

    def resolve(rel_path: str) -> str | None:
        # Walk up with each source in turn: a complete local checkout wins outright,
        # and only a checkout that cannot answer at all falls through to the API.
        for reader in readers:
            resolved = find_identity_with(reader, rel_path)
            if resolved:
                return resolved
        return None

    return resolve
