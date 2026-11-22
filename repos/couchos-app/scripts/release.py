#!/usr/bin/env python3
"""Version, build number and changelog of the CouchOS iOS app.

One source of truth: Config/Version.xcconfig (MARKETING_VERSION, the App Store
"Version", and CURRENT_PROJECT_VERSION, the "Build"). CHANGELOG.md has one
section per version, and every entry names the build that first had it.

    release.py show                 version and build
    release.py bump                 build + 1 (every change to the app)
    release.py set-version X.Y.Z    new version, opens its changelog section
                                    (only when the owner decides on a bump)
    release.py check                everything consistent? exit 1 if not
    release.py notes [--since N]    TestFlight "What to Test" / App Store
                                    "What's New" text for the current version
    release.py release [YYYY-MM-DD] date the current version's section
                                    (it went out on the App Store)

Python 3 standard library only. Paths are resolved from this file, so it can
be run from anywhere: python3 /abs/path/repos/couchos-app/scripts/release.py show
"""

from __future__ import annotations

import datetime as _dt
import re
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
XCCONFIG = APP / "Config" / "Version.xcconfig"
CHANGELOG = APP / "CHANGELOG.md"
PBXPROJ = APP / "CouchOS.xcodeproj" / "project.pbxproj"

SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
HEADING = re.compile(r"^## \[(?P<version>[^\]]+)\] — (?P<date>unreleased|\d{4}-\d{2}-\d{2})\s*$")
ENTRY_BUILD = re.compile(r"\(build (\d+)\)\s*$")
# App Store Connect limits "What to Test" and "What's New" to 4000 characters.
NOTES_LIMIT = 4000
CATEGORIES = ("Added", "Changed", "Fixed", "Removed")


class Problem(Exception):
    pass


# --- Version.xcconfig ---------------------------------------------------------

def read_versions() -> tuple[str, int]:
    text = XCCONFIG.read_text()
    marketing = re.search(r"^MARKETING_VERSION\s*=\s*(\S+)\s*$", text, re.M)
    build = re.search(r"^CURRENT_PROJECT_VERSION\s*=\s*(\S+)\s*$", text, re.M)
    if not marketing or not build:
        raise Problem(f"{XCCONFIG} must set MARKETING_VERSION and CURRENT_PROJECT_VERSION")
    if not SEMVER.match(marketing.group(1)):
        raise Problem(f"MARKETING_VERSION {marketing.group(1)!r} is not X.Y.Z (three integers, no leading zeros)")
    if not build.group(1).isdigit() or int(build.group(1)) < 1:
        raise Problem(f"CURRENT_PROJECT_VERSION {build.group(1)!r} must be a positive integer")
    return marketing.group(1), int(build.group(1))


def write_setting(key: str, value: str) -> None:
    text = XCCONFIG.read_text()
    new, count = re.subn(rf"^{key}\s*=.*$", f"{key} = {value}", text, flags=re.M)
    if count != 1:
        raise Problem(f"{key} must appear exactly once in {XCCONFIG}")
    XCCONFIG.write_text(new)


def semver_key(version: str) -> tuple[int, int, int]:
    major, minor, patch = (int(p) for p in version.split("."))
    return major, minor, patch


# --- CHANGELOG.md ---------------------------------------------------------------

class Section:
    def __init__(self, version: str, date: str, line: int):
        self.version = version
        self.date = date
        self.line = line
        self.lines: list[str] = []

    def entries(self) -> list[tuple[str | None, str, int | None]]:
        """(category, text, build) for every '- ' line."""
        out = []
        category = None
        for raw in self.lines:
            if raw.startswith("### "):
                category = raw[4:].strip()
            elif raw.startswith("- "):
                match = ENTRY_BUILD.search(raw)
                out.append((category, raw[2:].strip(), int(match.group(1)) if match else None))
        return out


def read_changelog() -> list[Section]:
    if not CHANGELOG.exists():
        raise Problem(f"{CHANGELOG} is missing")
    sections: list[Section] = []
    for number, raw in enumerate(CHANGELOG.read_text().splitlines(), start=1):
        if raw.startswith("## "):
            match = HEADING.match(raw)
            if not match:
                raise Problem(f"CHANGELOG.md:{number}: heading must be '## [X.Y.Z] — unreleased' or '## [X.Y.Z] — YYYY-MM-DD'")
            sections.append(Section(match["version"], match["date"], number))
        elif sections:
            sections[-1].lines.append(raw)
    return sections


# --- commands -----------------------------------------------------------------

def problems() -> list[str]:
    found: list[str] = []
    try:
        version, build = read_versions()
    except Problem as error:
        return [str(error)]

    pbx = PBXPROJ.read_text()
    for key in ("MARKETING_VERSION", "CURRENT_PROJECT_VERSION"):
        if re.search(rf"\b{key}\s*=", pbx):
            found.append(f"project.pbxproj sets {key}: it overrides Version.xcconfig. Delete it from the target's build settings.")
    if "baseConfigurationReference" not in pbx:
        found.append("project.pbxproj does not use Version.xcconfig as the target's base configuration")

    try:
        sections = read_changelog()
    except Problem as error:
        return found + [str(error)]
    if not sections:
        return found + ["CHANGELOG.md has no version sections"]

    top = sections[0]
    if top.version != version:
        found.append(f"CHANGELOG.md's newest section is {top.version}, but the app is {version}: run set-version, or fix the heading")
    seen_versions: list[str] = []
    for section in sections:
        if not SEMVER.match(section.version):
            found.append(f"CHANGELOG.md:{section.line}: {section.version!r} is not X.Y.Z")
            continue
        if section.version in seen_versions:
            found.append(f"CHANGELOG.md:{section.line}: {section.version} appears twice")
        if seen_versions and semver_key(section.version) >= semver_key(seen_versions[-1]):
            found.append(f"CHANGELOG.md:{section.line}: {section.version} must be lower than {seen_versions[-1]} above it")
        seen_versions.append(section.version)
        for category, text, entry_build in section.entries():
            if entry_build is None:
                found.append(f"CHANGELOG.md [{section.version}]: entry has no '(build N)' at its end: {text[:60]}")
            elif entry_build > build:
                found.append(f"CHANGELOG.md [{section.version}]: entry names build {entry_build}, newer than the app's build {build}")
            if category not in CATEGORIES:
                found.append(f"CHANGELOG.md [{section.version}]: entry is under '{category}', use one of {', '.join(CATEGORIES)}")
        if not section.entries():
            found.append(f"CHANGELOG.md [{section.version}]: section has no entries")

    builds = [b for _, _, b in top.entries() if b is not None]
    if top.version == version and build not in builds:
        found.append(f"CHANGELOG.md [{version}] has no entry for the current build {build}: describe what changed and end it with '(build {build})'")

    # Builds only go up: an older version's entries must not name a newer build
    # than the version above it.
    previous_min = None
    for section in sections:
        section_builds = [b for _, _, b in section.entries() if b is not None]
        if not section_builds:
            continue
        if previous_min is not None and max(section_builds) > previous_min:
            found.append(f"CHANGELOG.md [{section.version}]: names build {max(section_builds)}, newer than builds of the version above it")
        previous_min = min(section_builds)
    return found


def cmd_show(_: list[str]) -> int:
    version, build = read_versions()
    print(f"{version} ({build})")
    return 0


def cmd_bump(_: list[str]) -> int:
    version, build = read_versions()
    write_setting("CURRENT_PROJECT_VERSION", str(build + 1))
    print(f"{version} ({build}) -> {version} ({build + 1})")
    print(f"Now add the change to CHANGELOG.md under [{version}], ending with '(build {build + 1})'.")
    return 0


def cmd_set_version(args: list[str]) -> int:
    if len(args) != 1 or not SEMVER.match(args[0]):
        raise Problem("usage: release.py set-version X.Y.Z (three integers)")
    new = args[0]
    version, build = read_versions()
    if semver_key(new) <= semver_key(version):
        raise Problem(f"{new} must be higher than the current {version}")
    write_setting("MARKETING_VERSION", new)
    text = CHANGELOG.read_text()
    lines = text.splitlines(keepends=True)
    first = next((i for i, line in enumerate(lines) if line.startswith("## ")), len(lines))
    lines[first:first] = [f"## [{new}] — unreleased\n", "\n"]
    CHANGELOG.write_text("".join(lines))
    print(f"{version} ({build}) -> {new} ({build})")
    print(f"Opened '## [{new}] — unreleased' in CHANGELOG.md. Run bump next: the new version needs a new build.")
    return 0


def cmd_check(_: list[str]) -> int:
    found = problems()
    if found:
        for problem in found:
            print(f"✗ {problem}")
        return 1
    version, build = read_versions()
    print(f"✓ {version} ({build}): Version.xcconfig, project.pbxproj and CHANGELOG.md agree")
    return 0


def cmd_notes(args: list[str]) -> int:
    since = 0
    if args:
        if len(args) != 2 or args[0] != "--since" or not args[1].isdigit():
            raise Problem("usage: release.py notes [--since BUILD]")
        since = int(args[1])
    version, build = read_versions()
    section = next((s for s in read_changelog() if s.version == version), None)
    if section is None:
        raise Problem(f"CHANGELOG.md has no section for {version}")
    by_category: dict[str, list[str]] = {}
    for category, text, entry_build in section.entries():
        if entry_build is not None and entry_build <= since:
            continue
        by_category.setdefault(category or "Changed", []).append(ENTRY_BUILD.sub("", text).rstrip())
    out = [f"CouchOS {version} (build {build})"]
    for category in CATEGORIES:
        if category in by_category:
            out.append("")
            out.append(category)
            out.extend(f"• {line}" for line in by_category[category])
    text = "\n".join(out)
    print(text)
    if len(text) > NOTES_LIMIT:
        print(f"\n✗ {len(text)} characters: App Store Connect allows {NOTES_LIMIT}. Use --since.", file=sys.stderr)
        return 1
    return 0


def cmd_release(args: list[str]) -> int:
    date = args[0] if args else _dt.date.today().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise Problem("usage: release.py release [YYYY-MM-DD]")
    version, build = read_versions()
    text = CHANGELOG.read_text()
    old = f"## [{version}] — unreleased"
    if old not in text:
        raise Problem(f"no '{old}' heading in CHANGELOG.md")
    CHANGELOG.write_text(text.replace(old, f"## [{version}] — {date}", 1))
    print(f"[{version}] released {date} (last build {build})")
    return 0


COMMANDS = {
    "show": cmd_show,
    "bump": cmd_bump,
    "set-version": cmd_set_version,
    "check": cmd_check,
    "notes": cmd_notes,
    "release": cmd_release,
}


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in COMMANDS:
        print(__doc__.strip())
        return 2
    try:
        return COMMANDS[argv[1]](argv[2:])
    except Problem as error:
        print(f"✗ {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
