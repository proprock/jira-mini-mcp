"""Check that every place recording the current released version agrees.

`pyproject.toml`, both `version` fields in `server.json`, `uv.lock`'s own
package entry, and CHANGELOG.md's latest release heading and `[Unreleased]`
compare link must all name one version, so a bump cannot update one and
silently miss another. Runs as a commit hook (only when one of those files
changes) and in CI; exits 1 and lists each disagreement.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _search(pattern: str, text: str, flags: int = 0) -> str | None:
    match = re.search(pattern, text, flags)
    return match.group(1) if match else None


def collect_versions() -> dict[str, str | None]:
    """Each place's recorded version; None when the place is unreadable."""
    server_json = json.loads(_read("server.json"))
    pypi = [p for p in server_json["packages"] if p["registryType"] == "pypi"]
    changelog = _read("CHANGELOG.md")
    return {
        "pyproject.toml": tomllib.loads(_read("pyproject.toml"))["project"]["version"],
        "server.json (top level)": server_json["version"],
        "server.json (pypi package)": pypi[0]["version"] if pypi else None,
        "uv.lock": _search(r'name = "jira-mini-mcp"\nversion = "([\d.]+)"', _read("uv.lock")),
        "CHANGELOG.md (latest release)": _search(
            r"^## \[(\d+\.\d+\.\d+)\] - \d{4}-\d{2}-\d{2}", changelog, re.MULTILINE
        ),
        "CHANGELOG.md ([Unreleased] compare base)": _search(
            r"^\[Unreleased\]: .*/compare/v([\d.]+)\.\.\.HEAD$", changelog, re.MULTILINE
        ),
    }


def main() -> int:
    versions = collect_versions()
    expected = versions["pyproject.toml"]
    bad = {place: found for place, found in versions.items() if found != expected}
    if not bad:
        return 0
    print(f"Version drift: pyproject.toml says {expected!r}.", file=sys.stderr)
    for place, found in bad.items():
        print(f"  {place}: {found!r}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
