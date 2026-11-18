"""Publish a new version of the trading bots (owner only).

    .venv\\Scripts\\python.exe scripts\\release.py 1.1.0            # asks before publishing
    .venv\\Scripts\\python.exe scripts\\release.py 1.1.0 --dry-run  # show what it would do

Before running: list the changes under "## Unreleased" in CHANGELOG.md
(one "- " line each, written for the people who install the bots).

It then:
  1. moves those notes under "## 1.1.0 — <today>" and sets pyproject.toml's version;
  2. commits just those two files ("Release v1.1.0") and tags v1.1.0;
  3. pushes master and the tag to both repos (they're identical: public
     trading-bot-first and the private trading-bot), and creates the GitHub
     Release on trading-bot-first, the repo people install from, with the
     one-click installer (Install-Trading-Bots.cmd) attached: docs/SETUP.md
     links to releases/latest/download/Install-Trading-Bots.cmd.

Every installed copy finds the new tag at its next 8am check, shows it on
the Bot controls tab and offers Update now (src/tradingbot/updates.py).
"""
from __future__ import annotations

import re
import subprocess
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from tradingbot import updates  # noqa: E402

RELEASE_REPO = "rshukla29/trading-bot-first"  # public: people install from it; the GitHub Release goes here
RELEASE_URL = f"https://github.com/{RELEASE_REPO}.git"
MIRROR_URL = "https://github.com/rshukla29/trading-bot.git"  # private, identical copy
INSTALLER = REPO / "Install-Trading-Bots.cmd"  # attached to every Release (docs/SETUP.md, step 4)


def git(*args: str) -> str:
    out = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed:\n{out.stderr.strip()}")
    return out.stdout.strip()


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    if len(args) != 1 or not updates.parse(args[0]):
        sys.exit("Usage: scripts\\release.py X.Y.Z [--dry-run]")
    version = args[0].lstrip("v")
    tag = "v" + version

    if git("rev-parse", "--abbrev-ref", "HEAD") != "master":
        sys.exit("Switch to the master branch first.")
    if git("diff", "--cached", "--name-only"):
        sys.exit("There are staged changes: commit or unstage them first.")
    if git("status", "--porcelain", "--", "pyproject.toml", "CHANGELOG.md"):
        sys.exit("pyproject.toml or CHANGELOG.md has uncommitted edits: commit the Unreleased notes first.")

    have = updates.current()
    subprocess.run(["git", "-C", str(REPO), "fetch", "--tags", "--quiet", RELEASE_URL], check=False)
    newest = updates.latest_tag(git("tag", "--list", "v*").split())
    floor = max(updates.parse(have), updates.parse(newest) if newest else (0, 0, 0))
    if updates.parse(version) <= floor:
        sys.exit(f"{version} must be newer than {'.'.join(map(str, floor))}.")

    changelog = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    try:
        new_changelog, notes = updates.cut_release(changelog, version, date.today().isoformat())
    except ValueError as exc:
        sys.exit(str(exc))
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    new_pyproject, n = re.subn(r'(?m)^version\s*=\s*"[^"]*"', f'version = "{version}"', pyproject, count=1)
    if n != 1:
        sys.exit("Couldn't find version = \"...\" in pyproject.toml")

    print(f"Release {tag} (this copy was {have}) to {RELEASE_REPO}, with these notes:")
    for item in notes:
        print("  -", item)
    if dry:
        print("\n--dry-run: nothing changed.")
        return
    if input("\nPublish it? Every installed copy will be offered this update. Type yes: ").strip().lower() != "yes":
        sys.exit("Not published.")

    (REPO / "CHANGELOG.md").write_text(new_changelog, encoding="utf-8")
    (REPO / "pyproject.toml").write_text(new_pyproject, encoding="utf-8")
    notes_md = "\n".join(f"- {i}" for i in notes)
    git("commit", "-m", f"Release {tag}\n\n{notes_md}", "--", "pyproject.toml", "CHANGELOG.md")
    git("tag", "-a", tag, "-m", f"{tag}\n\n{notes_md}")
    for url in (RELEASE_URL, MIRROR_URL):
        git("push", url, "master:master", f"refs/tags/{tag}:refs/tags/{tag}")
    out = subprocess.run(["gh", "release", "create", tag, str(INSTALLER), "--repo", RELEASE_REPO, "--title", tag,
                          "--notes", notes_md, "--verify-tag"], capture_output=True, text=True)
    if out.returncode != 0:
        print(f"Tag pushed, but creating the GitHub Release failed (copies are still notified):\n{out.stderr.strip()}")
    else:
        print(out.stdout.strip())
    print(f"Published {tag}.")


if __name__ == "__main__":
    main()
