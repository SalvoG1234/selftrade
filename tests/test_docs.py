"""Documentation integrity: the front page's links and the docs index cannot quietly rot.

The README is the repository's front door and it points at a lot of files — screenshots, the
verification ledger, the contributing rules. A broken link there is worse than a missing
paragraph: it tells a reader the project is not maintained. These two tests are cheap and they
fail on the mistake a person actually makes (renaming a file and forgetting one of its links).

What is deliberately *not* checked: external URLs (a test must not need the network) and GitHub
routes such as ``issues/new``, which are links into the repository's own pages rather than files.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: Every markdown file a reader is expected to follow links out of.
MARKDOWN = [
    ROOT / "README.md",
    ROOT / "CHANGELOG.md",
    ROOT / "CONTRIBUTING.md",
    ROOT / "SECURITY.md",
    ROOT / "SUPPORT.md",
    *sorted((ROOT / "docs").glob("*.md")),
    ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md",
]

#: Suffixes that mean "this link should be a file that exists".
FILE_SUFFIXES = (".md", ".png", ".jpg", ".jpeg", ".svg", ".txt", ".yml", ".yaml", ".ps1", ".py", ".json", ".csv")

#: GitHub pages, not files: `](issues/new)` is a route inside this repository.
GITHUB_ROUTES = ("issues", "discussions", "pulls", "actions", "releases", "security")

_LINK = re.compile(r"\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_FENCE = re.compile(r"^```.*?^```", re.S | re.M)


def _local_targets(path: Path) -> list[str]:
    """Every link target in one file that should name a file, with its fragment removed."""
    body = _FENCE.sub("", path.read_text(encoding="utf-8"))
    out: list[str] = []
    for raw in _LINK.findall(body):
        target = raw.strip().strip("<>")
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        target = target.split("#", 1)[0]
        if not target:
            continue
        head = target.lstrip("./").split("/", 1)[0]
        if head in GITHUB_ROUTES:  # a route into the repository's own pages
            continue
        if target.startswith(("docs/", "../", "./")) or target.endswith(FILE_SUFFIXES):
            out.append(target)
    return out


def test_the_documents_exist():
    missing = [str(p.relative_to(ROOT)) for p in MARKDOWN if not p.exists()]
    assert missing == [], missing


def test_every_relative_link_resolves():
    """A renamed file must not leave a link pointing at nothing."""
    broken: list[str] = []
    checked = 0
    per_doc: dict[str, int] = {}
    for doc in MARKDOWN:
        if not doc.exists():  # covered by its own test, and it must not mask the real failure
            continue
        for target in _local_targets(doc):
            checked += 1
            per_doc[doc.name] = per_doc.get(doc.name, 0) + 1
            if not (doc.parent / target).resolve().exists():
                broken.append(f"{doc.relative_to(ROOT)} -> {target}")
    # A floor, not a target: it exists so that a scanner regression (a regex that stops matching)
    # cannot turn this test green by checking nothing. The README is the file that must stay
    # link-rich, because it is the one a reader arrives on.
    assert checked >= 60, f"only {checked} links were checked: the scanner stopped working"
    assert per_doc.get("README.md", 0) >= 10, per_doc
    assert broken == [], "broken relative links:\n  " + "\n  ".join(broken)


def test_every_document_is_in_the_index():
    """`docs/README.md` is the index, so an orphan document is a reader's dead end."""
    index = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    orphans = [
        p.name
        for p in sorted((ROOT / "docs").glob("*.md"))
        if p.name != "README.md" and f"({p.name})" not in index
    ]
    assert orphans == [], f"not listed in docs/README.md: {orphans}"


@pytest.mark.parametrize("image", ["dashboard-overview", "dashboard-api-keys", "dashboard-risk-center"])
def test_the_readme_screenshots_are_real_files(image: str):
    """The images the front page shows must exist and be a plausible render, not a placeholder."""
    path = ROOT / "docs" / "img" / f"{image}.png"
    assert path.exists(), path
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    # A blank or single-colour image of this size compresses to a few kilobytes; a real render of
    # the dashboard does not. This is a floor, not a quality check - the file is what was captured.
    assert len(data) > 40_000, f"{path.name} is only {len(data)} bytes: a capture would be larger"
