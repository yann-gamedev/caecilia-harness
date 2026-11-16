"""Write-set patterns: glob matching and conservative overlap detection (ADR-0013).

Pure logic, no IO. Patterns and paths are repo-relative and use `/` (the format of
`git diff --name-only`). Matching is case-sensitive.

- `*`  zero or more characters inside one segment (never crosses `/`)
- `?`  exactly one character other than `/`
- `**` whole segments: zero or more in the middle or at the start of a pattern, one or more at
  the end, so `a/**` matches everything inside `a` but not `a` itself.

Written by hand on purpose: `fnmatch`'s `*` crosses `/`, and `PurePath.full_match` needs
Python 3.13 while the project supports 3.12.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from functools import cache, lru_cache

WHOLE_REPO = "**"
_DRIVE = re.compile(r"[A-Za-z]:")


class InvalidPatternError(ValueError):
    """A write-set pattern that is not a safe, repo-relative glob."""


def validate_pattern(pattern: str) -> None:
    """Raise `InvalidPatternError` unless `pattern` is a repo-relative glob this module supports."""
    if not pattern:
        raise InvalidPatternError("pattern is empty")
    if "\\" in pattern:
        raise InvalidPatternError(f"use '/' instead of backslashes: {pattern!r}")
    if pattern.startswith("/"):
        raise InvalidPatternError(f"pattern must be relative to the repo root: {pattern!r}")
    if _DRIVE.match(pattern):
        raise InvalidPatternError(f"drive letters are not allowed: {pattern!r}")
    for segment in pattern.split("/"):
        if not segment:
            raise InvalidPatternError(f"empty path segment in {pattern!r}")
        if segment in (".", ".."):
            raise InvalidPatternError(f"'.' and '..' segments are not allowed: {pattern!r}")
        if "**" in segment and segment != "**":
            raise InvalidPatternError(f"'**' must be a whole segment: {pattern!r}")


@lru_cache(maxsize=4096)
def _segment_regex(segment: str) -> re.Pattern[str]:
    parts = []
    for char in segment:
        if char == "*":
            parts.append("[^/]*")
        elif char == "?":
            parts.append("[^/]")
        else:
            parts.append(re.escape(char))
    return re.compile("".join(parts))


def _match_segments(path: tuple[str, ...], pattern: tuple[str, ...]) -> bool:
    @cache
    def go(i: int, j: int) -> bool:
        if j == len(pattern):
            return i == len(path)
        if pattern[j] == "**":
            if j == len(pattern) - 1:
                return i < len(path)  # trailing **: at least one segment must remain
            return any(go(k, j + 1) for k in range(i, len(path) + 1))
        if i == len(path):
            return False
        return _segment_regex(pattern[j]).fullmatch(path[i]) is not None and go(i + 1, j + 1)

    return go(0, 0)


def matches(path: str, pattern: str) -> bool:
    """True if repo-relative `path` matches `pattern`. Raises on an invalid pattern."""
    validate_pattern(pattern)
    segments = path.split("/")
    if not path or "" in segments:
        return False
    return _match_segments(tuple(segments), tuple(pattern.split("/")))


def normalize_write_set(patterns: Sequence[str]) -> list[str]:
    """Validate, drop duplicates (keeping order); an empty write set means the whole repo."""
    normalized: list[str] = []
    for pattern in patterns:
        validate_pattern(pattern)
        if pattern not in normalized:
            normalized.append(pattern)
    return normalized or [WHOLE_REPO]


def _literal_prefix(pattern: str) -> list[str]:
    """Segments before the first one containing a wildcard."""
    prefix: list[str] = []
    for segment in pattern.split("/"):
        if "*" in segment or "?" in segment:
            break
        prefix.append(segment)
    return prefix


def _provably_disjoint(a: str, b: str) -> bool:
    # Every path matching a pattern starts with that pattern's literal prefix, segment by
    # segment. If the two prefixes disagree at a position both have, no path can match both.
    return any(x != y for x, y in zip(_literal_prefix(a), _literal_prefix(b), strict=False))


def may_overlap(a: Sequence[str], b: Sequence[str]) -> bool:
    """Whether two write sets might touch the same file.

    Conservative: returns False only when every pair of patterns is provably disjoint, so
    "False" is always safe to act on while "True" may be a false alarm (tasks then run serially).
    """
    return any(
        not _provably_disjoint(x, y) for x in normalize_write_set(a) for y in normalize_write_set(b)
    )
