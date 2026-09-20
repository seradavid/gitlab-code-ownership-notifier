"""CODEOWNERS parsing with GitLab-compatible semantics.

Semantics encoded here (docs/design.md §7.1) — these are subtle, so they are
deliberately explicit and covered by tests:

* **The last matching pattern wins** (order in the file is meaningful).
* ``*`` does not cross ``/``; ``**`` does.
* ``?``, ``[abc]`` and ``{a,b}`` are supported.
* ``!`` negation is **not** supported. Ignore lists therefore live in ``teams.yml``
  and are applied *before* CODEOWNERS matching.
* Sections (``[Name]``) are grouping labels only; they are recorded, not matched.
* A pattern without a ``/`` matches at any depth; one containing a ``/`` is anchored
  to the repository root (the same rule as a root ``.gitignore``).
* A path matching nothing is **unclaimed**.
* Invalid lines are reported, never silently interpreted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Locations GitLab looks for, in its documented search order.
CODEOWNERS_PATHS = ("CODEOWNERS", ".gitlab/CODEOWNERS", "docs/CODEOWNERS")

_REGEX_SPECIAL = ".^$+()|\\"

#: Owner tokens: @user, @group, @group/subgroup, or an email address.
#: Two deliberately simple patterns instead of one alternation: overlapping character
#: classes make a single pattern backtrack on non-matching input, and this runs once per
#: owner token on every changed file.
_AT_TOKEN_RE = re.compile(r"^@[A-Za-z0-9_.\-/]+$")
_EMAIL_RE = re.compile(r"^[^@\s.]+(?:\.[^@\s.]+)*@[^@\s.]+(?:\.[^@\s.]+)+$")


def is_owner_token(token: str) -> bool:
    """True for ``@user`` / ``@group/subgroup`` or an email address."""
    pattern = _AT_TOKEN_RE if token.startswith("@") else _EMAIL_RE
    return pattern.match(token) is not None

_SECTION_RE = re.compile(r"^\^?\[(?P<name>[^\]]+)\]$")


@dataclass(frozen=True)
class _Piece:
    """One translated fragment of a pattern, plus where the scan resumes."""

    text: str
    next_index: int


def _translate_star(p: str, i: int) -> _Piece:
    """``**/`` (zero or more directories), ``/**`` / ``**`` (anything), or ``*``."""
    if i + 1 < len(p) and p[i + 1] == "*":
        if i + 2 < len(p) and p[i + 2] == "/":
            return _Piece("(?:.*/)?", i + 3)
        return _Piece(".*", i + 2)
    return _Piece("[^/]*", i + 1)


def _translate_bracket(p: str, i: int) -> _Piece:
    """``[abc]`` character class; literal if the class is unterminated."""
    end = p.find("]", i + 1)
    if end == -1:
        return _Piece(re.escape(p[i]), i + 1)
    return _Piece(p[i : end + 1], end + 1)


def _translate_brace(p: str, i: int) -> _Piece:
    end = p.find("}", i + 1)
    if end == -1:
        return _Piece(re.escape(p[i]), i + 1)
    alternatives = p[i + 1 : end].split(",")
    return _Piece(f"(?:{'|'.join(re.escape(a) for a in alternatives)})", end + 1)


def _translate_char(p: str, i: int) -> _Piece:
    char = p[i]
    if char == "*":
        return _translate_star(p, i)
    if char == "?":
        return _Piece("[^/]", i + 1)
    if char == "[":
        return _translate_bracket(p, i)
    if char == "{":
        return _translate_brace(p, i)
    if char in _REGEX_SPECIAL:
        return _Piece("\\" + char, i + 1)
    return _Piece(char, i + 1)


def translate_pattern(pattern: str) -> re.Pattern[str]:
    """Turn a CODEOWNERS pattern into a regex, following GitLab/.gitignore rules."""

    anchored = pattern.startswith("/")
    p = pattern[1:] if anchored else pattern
    if p.endswith("/"):
        p = p + "**"

    has_inner_slash = "/" in p.strip("/")

    out: list[str] = []
    i = 0
    while i < len(p):
        piece = _translate_char(p, i)
        out.append(piece.text)
        i = piece.next_index

    prefix = "^" if (anchored or has_inner_slash) else "(?:^|.*/)"
    return re.compile(prefix + "".join(out) + "$")


@dataclass(frozen=True)
class Rule:
    """One ``pattern owners...`` line."""

    pattern: str
    owners: tuple[str, ...]
    line: int
    section: str | None = None
    regex: re.Pattern[str] = field(compare=False, default=re.compile(""))
    raw: str = ""


@dataclass
class ParsedCodeowners:
    rules: list[Rule] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)

    def rule_for(self, path: str) -> Rule | None:
        """Return the rule that wins for ``path`` — the *last* matching one."""
        winner: Rule | None = None
        for rule in self.rules:
            if rule.regex.match(path):
                winner = rule
        return winner

    def owners_for(self, path: str) -> tuple[str, ...]:
        rule = self.rule_for(path)
        return rule.owners if rule else ()

    @property
    def is_empty(self) -> bool:
        return not self.rules

    def all_patterns(self) -> list[str]:
        return [rule.pattern for rule in self.rules]


def _strip_comment(line: str) -> str:
    """Drop a trailing ``#`` comment (a leading one is a whole-line comment)."""
    if "#" not in line:
        return line
    return line.split("#", 1)[0].strip()


def _rule_from_line(
    number: int, line: str, raw_line: str, section: str | None
) -> tuple[Rule | None, list[str]]:
    """Build one rule, or explain why the line cannot be used."""
    parts = line.split()
    pattern, owners = parts[0], parts[1:]

    if pattern.startswith("!"):
        return None, [
            f"line {number}: negation ('!') is not supported by CODEOWNERS; "
            "put ignore patterns in teams.yml instead"
        ]
    if not owners:
        return None, [f"line {number}: pattern '{pattern}' has no owner"]

    problems: list[str] = []
    bad = [owner for owner in owners if not is_owner_token(owner)]
    if bad:
        problems.append(
            f"line {number}: unrecognised owner token(s) {bad!r} "
            "(expected @user, @group or an email address)"
        )

    try:
        regex = translate_pattern(pattern)
    except re.error as exc:  # pragma: no cover - defensive
        return None, problems + [f"line {number}: could not compile '{pattern}': {exc}"]

    rule = Rule(
        pattern=pattern,
        owners=tuple(owners),
        line=number,
        section=section,
        regex=regex,
        raw=raw_line,
    )
    return rule, problems


def parse(text: str) -> ParsedCodeowners:
    """Parse CODEOWNERS content. Never raises: problems are collected."""
    parsed = ParsedCodeowners()
    section: str | None = None

    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        section_match = _SECTION_RE.match(line)
        if section_match:
            section = section_match.group("name").strip()
            parsed.sections.append(section)
            continue

        line = _strip_comment(line)
        if not line:
            continue

        rule, problems = _rule_from_line(number, line, raw_line, section)
        parsed.problems.extend(problems)
        if rule is not None:
            parsed.rules.append(rule)

    return parsed


@dataclass
class TeamPatternDelta:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.added and not self.removed


def team_pattern_delta(
    before: ParsedCodeowners,
    after: ParsedCodeowners,
    token_to_team: dict[str, str],
) -> dict[str, TeamPatternDelta]:
    """Per-team difference between two revisions of a CODEOWNERS file.

    Used by the ownership-change alert (§9.4): the team being *removed* is the one
    with something to lose, and it is the only case where a team is affected by an
    MR that touches none of its files.
    """

    def by_team(parsed: ParsedCodeowners) -> dict[str, set[str]]:
        result: dict[str, set[str]] = {}
        for rule in parsed.rules:
            for token in rule.owners:
                team = token_to_team.get(token)
                if team:
                    result.setdefault(team, set()).add(rule.pattern)
                else:
                    result.setdefault(f"__unknown__{token}", set()).add(rule.pattern)
        return result

    old, new = by_team(before), by_team(after)
    delta: dict[str, TeamPatternDelta] = {}
    for team in set(old) | set(new):
        added = sorted(new.get(team, set()) - old.get(team, set()))
        removed = sorted(old.get(team, set()) - new.get(team, set()))
        if added or removed:
            delta[team] = TeamPatternDelta(added=added, removed=removed)
    return delta


def unknown_owner_tokens(parsed: ParsedCodeowners, token_to_team: dict[str, str]) -> list[str]:
    """Owner tokens with no matching team entry in ``teams.yml``."""
    unknown: set[str] = set()
    for rule in parsed.rules:
        for token in rule.owners:
            if token not in token_to_team:
                unknown.add(token)
    return sorted(unknown)
