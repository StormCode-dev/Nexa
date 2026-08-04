#!/usr/bin/env python3
"""
Version bump + build CLI for Nexa

Usage:
    python nexatool.py -verName infer
    python nexatool.py -verName "0.3.1-beta"
    python nexatool.py -verName infer -onlyBump
    python nexatool.py -verName infer -onlyCompile
    python nexatool.py -verName "0.3.1-beta" -onlyCompile
"""

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# This script lives in dTs/, one level below the repo root.
ROOT = Path(__file__).resolve().parent.parent

MAIN_PY = ROOT / "src" / "main.py"
DISCORD_BOT_PY = ROOT / "src" / "bot" / "discordBot.py"
SYSTEM_PY = ROOT / "src" / "bot" / "cogs" / "system.py"
UPDATE_INDEX_JSON = ROOT / "updateIndex.json"

NUITKA_ENTRY = ROOT / "src" / "main.py"
DIST_DIR = "dist"
COMPANY_NAME = "StormCode"
PRODUCT_NAME = "Nexa"
COPYRIGHT = "Copyright (c) 2026 StormCode & Contributors"
PINGGY_DLL_SRC = ROOT / ".venv" / "Lib" / "site-packages" / "pinggy" / "bin" / "pinggy.dll"
PINGGY_DLL_DEST = "pinggy\\bin\\pinggy.dll"

DEFAULT_SUFFIX = "beta"

VERSION_STRING_RE = re.compile(
    r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)(?:-(?P<suffix>[A-Za-z0-9_.]+))?$"
)


def _diagnose_version_string(version_string: str) -> str:
    """Return a human-readable explanation of which part of version_string is invalid."""
    stripped = version_string.strip()
    problems: list[str] = []

    if stripped.startswith("nexa-v"):
        problems.append(
            "starts with 'nexa-v' - the stored identifier should be bare, e.g. "
            "'0.3.0-beta.hotfix2' (no prefix); the 'Nexa v' / 'nexa-v' label is "
            "added automatically only for display contexts like the exe filename"
        )
        stripped = stripped[len("nexa-v"):]

    if "-" in stripped:
        core, suffix = stripped.split("-", 1)
    else:
        core, suffix = stripped, None

    parts = core.split(".")

    def _check_numeric(label: str, value: str | None) -> None:
        if value is None or value == "":
            problems.append(f"{label} is missing")
        elif not value.isdigit():
            problems.append(f"{label} must be a whole number, got '{value}'")

    if len(parts) >= 1:
        _check_numeric("major version", parts[0])
    else:
        problems.append("major version is missing")

    if len(parts) >= 2:
        _check_numeric("minor version", parts[1])
    else:
        problems.append("minor version is missing")

    if len(parts) >= 3:
        _check_numeric("patch version", parts[2])
    else:
        problems.append("patch version is missing")

    if len(parts) > 3:
        problems.append(
            f"too many '.'-separated segments in the version core "
            f"(found {len(parts)}, expected exactly 3: major.minor.patch)"
        )

    if suffix is not None and not re.fullmatch(r"[A-Za-z0-9_.]+", suffix):
        problems.append(
            f"suffix '{suffix}' contains invalid characters "
            f"(only letters, numbers, '_', and '.' are allowed)"
        )

    if not problems:
        problems.append("format does not match '<major>.<minor>.<patch>[-<suffix>]' for an unspecified reason")

    return "; ".join(problems)


class NexaToolError(Exception):
    """Raised for any condition that should abort the tool before writes happen."""


@dataclass
class NexaVersion:
    major: int
    minor: int
    patch: int
    suffix: str | None

    @staticmethod
    def parse(version_string: str) -> "NexaVersion":
        m = VERSION_STRING_RE.match(version_string.strip())
        if not m:
            reason = _diagnose_version_string(version_string)
            raise NexaToolError(
                f"Version string '{version_string}' does not match required pattern "
                f"'<major>.<minor>.<patch>[-<suffix>]': {reason}. "
                f"Aborting, no files touched."
            )
        return NexaVersion(
            major=int(m.group("major")),
            minor=int(m.group("minor")),
            patch=int(m.group("patch")),
            suffix=m.group("suffix"),
        )

    def bumped_patch(self, new_suffix: str = DEFAULT_SUFFIX) -> "NexaVersion":
        return NexaVersion(self.major, self.minor, self.patch + 1, new_suffix)

    @property
    def full_string(self) -> str:
        """
        e.g. '0.3.0-beta.hotfix2'
        This is the bare identifier stored identically in all 4 source-of-truth
        sites: main.py, discordBot.py, system.py, updateIndex.json.
        """
        base = f"{self.major}.{self.minor}.{self.patch}"
        return f"{base}-{self.suffix}" if self.suffix else base

    @property
    def display_string(self) -> str:
        """e.g. 'nexa-v0.3.0-beta.hotfix2'. Used for the exe filename only."""
        return f"nexa-v{self.full_string}"

    @property
    def display_label(self) -> str:
        """e.g. 'Nexa v0.3.0-beta.hotfix2'. Used for --file-description only."""
        return f"Nexa v{self.full_string}"

    @property
    def numeric_full(self) -> str:
        """e.g. 0.3.1.0 - Strict Windows file-version format required by Nuitka."""
        return f"{self.major}.{self.minor}.{self.patch}.0"



def _replace_single_match(path: Path, pattern: re.Pattern, replacement: str, label: str) -> str:
    if not path.exists():
        raise NexaToolError(f"Expected file not found: {path}")
    text = path.read_text(encoding="utf-8")
    matches = list(pattern.finditer(text))
    if len(matches) == 0:
        raise NexaToolError(
            f"Could not find {label} pattern in {path}. "
            f"File format may have changed. Aborting, no files touched."
        )
    if len(matches) > 1:
        raise NexaToolError(
            f"Found {len(matches)} matches for {label} pattern in {path}, expected exactly 1. "
            f"Ambiguous edit target. Aborting, no files touched."
        )
    m = matches[0]
    new_text = text[: m.start()] + replacement + text[m.end() :]
    return new_text


def build_main_py_edit(version: NexaVersion) -> tuple[Path, str]:
    pattern = re.compile(r'currentNexaVersion\s*=\s*["\'][^"\']*["\']')
    replacement = f'currentNexaVersion = "{version.full_string}"'
    new_text = _replace_single_match(MAIN_PY, pattern, replacement, "currentNexaVersion")
    return MAIN_PY, new_text


def build_discord_bot_py_edit(version: NexaVersion) -> tuple[Path, str]:
    pattern = re.compile(r'VERSION\s*=\s*["\'][^"\']*["\']')
    replacement = f'VERSION = "{version.full_string}"'
    new_text = _replace_single_match(DISCORD_BOT_PY, pattern, replacement, "VERSION")
    return DISCORD_BOT_PY, new_text


def build_system_py_edit(version: NexaVersion) -> tuple[Path, str]:
    pattern = re.compile(r'CURRENT_NEXA_VERSION\s*=\s*["\'][^"\']*["\']')
    replacement = f'CURRENT_NEXA_VERSION = "{version.full_string}"'
    new_text = _replace_single_match(SYSTEM_PY, pattern, replacement, "CURRENT_NEXA_VERSION")
    return SYSTEM_PY, new_text


def read_update_index() -> dict:
    if not UPDATE_INDEX_JSON.exists():
        raise NexaToolError(f"Expected file not found: {UPDATE_INDEX_JSON}")
    try:
        return json.loads(UPDATE_INDEX_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise NexaToolError(f"{UPDATE_INDEX_JSON} is not valid JSON: {e}")


def build_update_index_edit(version: NexaVersion) -> tuple[Path, str]:
    data = read_update_index()
    if "latestNexaVersion" not in data:
        raise NexaToolError(
            f"Key 'latestNexaVersion' not found in {UPDATE_INDEX_JSON}. Aborting, no files touched."
        )
    data["latestNexaVersion"] = version.full_string
    new_text = json.dumps(data, indent=2) + "\n"
    return UPDATE_INDEX_JSON, new_text


def infer_next_version() -> NexaVersion:
    data = read_update_index()
    if "latestNexaVersion" not in data:
        raise NexaToolError(
            f"Cannot infer: key 'latestNexaVersion' not found in {UPDATE_INDEX_JSON}."
        )
    current = NexaVersion.parse(data["latestNexaVersion"])
    return current.bumped_patch(new_suffix=DEFAULT_SUFFIX)


def resolve_version(ver_name_arg: str) -> NexaVersion:
    if ver_name_arg.strip().lower() == "infer":
        return infer_next_version()
    return NexaVersion.parse(ver_name_arg)


def current_version_for_compile_only() -> NexaVersion:
    data = read_update_index()
    if "latestNexaVersion" not in data:
        raise NexaToolError(
            f"Cannot compile: key 'latestNexaVersion' not found in {UPDATE_INDEX_JSON}."
        )
    return NexaVersion.parse(data["latestNexaVersion"])


def do_bump(version: NexaVersion) -> None:
    """Compute all edits first, validate all of them, THEN write. No partial writes."""
    edits = [
        build_main_py_edit(version),
        build_discord_bot_py_edit(version),
        build_system_py_edit(version),
        build_update_index_edit(version),
    ]
    # All edits succeeded in memory -> now commit to disk.
    for path, new_text in edits:
        path.write_text(new_text, encoding="utf-8")
        print(f"  updated: {path.relative_to(ROOT)}")

    _verify_all_sites_agree(version)

    print(f"\nVersion bumped to {version.full_string} across all files. All sources agree.")


def _verify_all_sites_agree(version: NexaVersion) -> None:
    expected = version.full_string

    checks: list[tuple[str, str]] = [
        ("src/main.py (currentNexaVersion)", _extract_quoted_value(MAIN_PY, r'currentNexaVersion\s*=\s*"([^"]*)"')),
        ("src/bot/discordBot.py (VERSION)", _extract_quoted_value(DISCORD_BOT_PY, r'VERSION\s*=\s*"([^"]*)"')),
        ("src/bot/cogs/system.py (CURRENT_NEXA_VERSION)", _extract_quoted_value(SYSTEM_PY, r'CURRENT_NEXA_VERSION\s*=\s*"([^"]*)"')),
        ("updateIndex.json (latestNexaVersion)", read_update_index().get("latestNexaVersion")),
    ]

    mismatches = [f"{label} = '{actual}'" for label, actual in checks if actual != expected]

    if mismatches:
        raise NexaToolError(
            f"Post-write verification failed: files were written but do not all agree. "
            f"Expected '{expected}' everywhere. Mismatches: " + "; ".join(mismatches) +
            ". This should not happen. Please submit a bug report. The codebase versions are now in an inconsistent state."
        )


def _extract_quoted_value(path: Path, pattern: str) -> str | None:
    text = path.read_text(encoding="utf-8")
    m = re.search(pattern, text)
    return m.group(1) if m else None


def do_compile(version: NexaVersion) -> None:
    if not PINGGY_DLL_SRC.exists():
        raise NexaToolError(
            f"pinggy.dll not found at {PINGGY_DLL_SRC}. "
            f"Check your venv is set up before compiling."
        )

    output_filename = f"{version.display_string}.exe"

    cmd = [
        sys.executable, "-m", "nuitka",
        "--onefile",
        f"--output-filename={output_filename}",
        f"--output-dir={DIST_DIR}",
        f"--company-name={COMPANY_NAME}",
        f"--product-name={PRODUCT_NAME}",
        f"--file-version={version.numeric_full}",
        f"--product-version={version.numeric_full}",
        f"--file-description={version.display_label}",
        f"--copyright={COPYRIGHT}",
        "--assume-yes-for-downloads",
        "--follow-imports",
        f"--include-data-files={PINGGY_DLL_SRC}={PINGGY_DLL_DEST}",
        str(NUITKA_ENTRY),
    ]

    print(f"Building {output_filename} ...")
    print("  " + " ".join(cmd))
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        raise NexaToolError(f"Nuitka build failed with exit code {result.returncode}.")
    print(f"\nBuild complete: {DIST_DIR}/{output_filename}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Nexa version bump + build tool")
    parser.add_argument(
        "-verName",
        dest="ver_name",
        default=None,
        help='Either "infer" or an explicit version string like "nexa-v0.3.1-beta". '
             "Ignored when -onlyCompile is used without an explicit override.",
    )
    parser.add_argument(
        "-onlyBump",
        dest="only_bump",
        action="store_true",
        help="Only update version references. Do not build.",
    )
    parser.add_argument(
        "-onlyCompile",
        dest="only_compile",
        action="store_true",
        help="Only build, using the version currently in updateIndex.json. "
             "-verName is optional in this mode; if given, it's used instead.",
    )
    args = parser.parse_args()

    if args.only_bump and args.only_compile:
        parser.error("-onlyBump and -onlyCompile are mutually exclusive.")

    try:
        if args.only_compile:
            version = (
                resolve_version(args.ver_name) if args.ver_name else current_version_for_compile_only()
            )
            do_compile(version)
            return 0

        # Bump is required for both "-onlyBump" and default (bump + compile) modes.
        if not args.ver_name:
            raise NexaToolError("-verName is required unless using -onlyCompile.")
        version = resolve_version(args.ver_name)

        do_bump(version)

        if not args.only_bump:
            do_compile(version)

        return 0

    except NexaToolError as e:
        print(f"\n[ABORTED] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())