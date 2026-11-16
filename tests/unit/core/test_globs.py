"""Write-set glob matching and conservative overlap detection (backlog M2-01, ADR-0013)."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from caecilia_harness.core.globs import (
    InvalidPatternError,
    matches,
    may_overlap,
    normalize_write_set,
    validate_pattern,
)

MATCH_CASES = [
    # (path, pattern, expected)
    ("a.md", "a.md", True),
    ("a.md", "b.md", False),
    ("d/a.md", "d/a.md", True),
    ("x.md", "*.md", True),
    ("d/x.md", "*.md", False),  # * never crosses a slash
    (".md", "*.md", True),  # * may match zero characters
    ("src/app.py", "src/*.py", True),
    ("src/pkg/app.py", "src/*.py", False),
    ("src/app.pyc", "src/*.py", False),
    ("a1.txt", "a?.txt", True),
    ("a.txt", "a?.txt", False),
    ("a/b", "a?b", False),  # ? never matches a slash
    ("a/x", "a/**", True),
    ("a/x/y", "a/**", True),
    ("a", "a/**", False),  # the folder itself is not inside it
    ("b/x", "a/**", False),
    ("x.py", "**/*.py", True),  # ** may match zero segments
    ("d/e/x.py", "**/*.py", True),
    ("d/e/x.txt", "**/*.py", False),
    ("x", "**", True),
    ("d/e/f", "**", True),
    ("a/b", "a/**/b", True),
    ("a/x/y/b", "a/**/b", True),
    ("a/x/y/c", "a/**/b", False),
    ("tests/api/test_x.py", "tests/**/test_*.py", True),
    ("tests/test_x.py", "tests/**/test_*.py", True),
    ("README.md", "readme.md", False),  # case-sensitive
    ("Src/a.py", "src/*.py", False),
    ("a/b/c", "a/*/c", True),
    ("a/b/d/c", "a/*/c", False),
    ("a/b", "a/b/c", False),
    ("a/b/c", "a/b", False),
    ("", "**", False),
]


@pytest.mark.parametrize(("path", "pattern", "expected"), MATCH_CASES)
def test_matches(path: str, pattern: str, expected: bool) -> None:
    assert matches(path, pattern) is expected


@pytest.mark.parametrize(
    "pattern",
    [
        "",
        "/etc/passwd",
        "/src/**",
        "C:/src/**",
        "c:src",
        "src\\app.py",
        "src/../secret",
        "..",
        "./src",
        "src/./a",
        "a**",
        "**a",
        "src/a**/b",
        "src/***",
        "src//a",
        "src/",
    ],
)
def test_validate_pattern_rejects(pattern: str) -> None:
    with pytest.raises(InvalidPatternError):
        validate_pattern(pattern)
    with pytest.raises(InvalidPatternError):
        normalize_write_set(["ok/**", pattern])


@pytest.mark.parametrize(
    "pattern", ["a", "a/b.py", "*.md", "src/**", "**", "**/*.py", "a/**/b", "a?c/d*", ".github/**"]
)
def test_validate_pattern_accepts(pattern: str) -> None:
    validate_pattern(pattern)


def test_normalize_write_set() -> None:
    assert normalize_write_set([]) == ["**"]
    assert normalize_write_set(["b/**", "a/**", "b/**", "a/**"]) == ["b/**", "a/**"]
    assert normalize_write_set(("src/*.py",)) == ["src/*.py"]


DISJOINT = [
    (["api/**"], ["web/**"]),
    (["api/**", "tests/api/**"], ["web/**", "tests/web/**"]),
    (["src/a.txt"], ["src/b.txt"]),
    (["src/api/*.py"], ["src/web/*.py"]),
    (["docs/a/**"], ["docs/b/x.md"]),
]
OVERLAPPING = [
    (["**"], ["web/**"]),
    ([], ["web/**"]),  # empty = whole repo
    ([], []),
    (["src/*.py"], ["src/a.txt"]),  # not provably disjoint -> treated as overlapping
    (["src/**"], ["src/web/**"]),
    (["*/a"], ["b/a"]),
    (["api/**"], ["web/**", "api/x.py"]),
    (["a/b"], ["a/b/**"]),
    (["**/*.py"], ["docs/**"]),
    (["x.md"], ["x.md"]),
]


@pytest.mark.parametrize(("a", "b"), DISJOINT)
def test_provably_disjoint_write_sets(a: list[str], b: list[str]) -> None:
    assert may_overlap(a, b) is False
    assert may_overlap(b, a) is False


@pytest.mark.parametrize(("a", "b"), OVERLAPPING)
def test_possibly_overlapping_write_sets(a: list[str], b: list[str]) -> None:
    assert may_overlap(a, b) is True
    assert may_overlap(b, a) is True


def test_may_overlap_rejects_invalid_patterns() -> None:
    with pytest.raises(InvalidPatternError):
        may_overlap(["../x"], ["a/**"])


# --- property: "disjoint" is never wrong -----------------------------------------------------------

_SEGMENTS = st.sampled_from(["a", "b", "ab", "c.py"])
_PATTERN_SEGMENTS = st.sampled_from(["a", "b", "ab", "c.py", "*", "?", "a*", "*.py", "?b", "**"])
_paths = st.lists(_SEGMENTS, min_size=1, max_size=4).map("/".join)
_patterns = st.lists(_PATTERN_SEGMENTS, min_size=1, max_size=4).map("/".join)
_write_sets = st.lists(_patterns, min_size=0, max_size=3)


@settings(max_examples=400, deadline=None)
@given(a=_write_sets, b=_write_sets, paths=st.lists(_paths, min_size=1, max_size=30))
def test_disjoint_write_sets_never_share_a_path(
    a: list[str], b: list[str], paths: list[str]
) -> None:
    assert may_overlap(a, b) is may_overlap(b, a)
    if may_overlap(a, b):
        return
    norm_a, norm_b = normalize_write_set(a), normalize_write_set(b)
    for path in paths:
        in_a = any(matches(path, pattern) for pattern in norm_a)
        in_b = any(matches(path, pattern) for pattern in norm_b)
        assert not (in_a and in_b), (path, a, b)


@settings(max_examples=200, deadline=None)
@given(path=_paths)
def test_double_star_matches_every_path(path: str) -> None:
    assert matches(path, "**")
    assert matches(path, "**/" + path.rsplit("/", 1)[-1])
