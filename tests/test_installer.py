"""The one-click installer (Install-Trading-Bots.cmd, docs/SETUP.md step 4)."""
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CMD = REPO / "Install-Trading-Bots.cmd"


def test_installer_is_a_crlf_ascii_batch_file():
    raw = CMD.read_bytes()
    assert raw.startswith(b"@echo off")
    assert all(b < 128 for b in raw)                              # PowerShell 5.1 reads it as ANSI
    assert raw.count(b"\n") == raw.count(b"\r\n")                 # cmd.exe needs CRLF
    assert raw.count(b"##PS-START") == 1                          # the marker the launcher looks for


def test_installer_clones_the_release_repo_and_runs_install_ps1():
    import importlib.util
    spec = importlib.util.spec_from_file_location("release", REPO / "scripts" / "release.py")
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)
    text = CMD.read_text(encoding="ascii")
    assert f'$RepoUrl = "{release.RELEASE_URL}"' in text
    assert release.INSTALLER == CMD                               # attached to every GitHub Release
    assert "scripts\install.ps1" in text and (REPO / "scripts" / "install.ps1").exists()
