#!/usr/bin/env python3
"""Install PM4K (script.plexmod) and its dependencies into a Kodi profile.

PM4K comes from a pinned GitHub commit; its dependencies come from the official
Kodi add-on repository. Kodi keeps unknown local add-ons disabled, so the
`enable` step turns them on through JSON-RPC once Kodi is running.

  kodi-addons.py install --addons-dir ~/.kodi/addons --repo URL --pm4k-ref SHA
  kodi-addons.py enable  --addons-dir ~/.kodi/addons --jsonrpc URL [--extra ID ...]

Prints one JSON line: {"changed": bool, "order": [...]}. Standard library only.
"""

import argparse
import gzip
import io
import json
import shutil
import sys
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

PM4K_ID = "script.plexmod"
PM4K_ZIP = "https://codeload.github.com/pannal/plex-for-kodi/zip/{ref}"
REF_MARKER = ".couchos-ref"
BUILTIN_PREFIXES = ("xbmc.", "kodi.")


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "couchos-kodi-addons/1"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def requires(addon_xml: ET.Element) -> list[str]:
    return [
        imp.get("addon")
        for imp in addon_xml.iterfind("./requires/import")
        if imp.get("optional") != "true" and not imp.get("addon", "").startswith(BUILTIN_PREFIXES)
    ]


def repo_index(repo: str) -> dict[str, ET.Element]:
    root = ET.fromstring(gzip.decompress(fetch(f"{repo}/addons.xml.gz")))
    return {a.get("id"): a for a in root.iterfind("addon")}


def extract_single_dir(data: bytes, dest: Path) -> None:
    """Extract a zip whose content is one top-level folder into `dest`."""
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            if name.startswith("/") or ".." in Path(name).parts:
                raise ValueError(f"unsafe path in zip: {name}")
        zf.extractall(tmp)
        (top,) = [p for p in Path(tmp).iterdir() if p.is_dir()]
        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(top), dest)


def install(args: argparse.Namespace) -> dict:
    addons = Path(args.addons_dir).expanduser()
    addons.mkdir(parents=True, exist_ok=True)
    changed = False

    pm4k = addons / PM4K_ID
    marker = pm4k / REF_MARKER
    if not marker.exists() or marker.read_text().strip() != args.pm4k_ref:
        extract_single_dir(fetch(PM4K_ZIP.format(ref=args.pm4k_ref)), pm4k)
        marker.write_text(args.pm4k_ref + "\n")
        changed = True

    index = None
    order: list[str] = []
    seen: set[str] = set()

    def visit(addon_id: str, xml: ET.Element) -> None:
        nonlocal index, changed
        for dep in requires(xml):
            if dep in seen:
                continue
            seen.add(dep)
            dep_dir = addons / dep
            if not (dep_dir / "addon.xml").exists():
                if index is None:
                    index = repo_index(args.repo)
                if dep not in index:
                    raise SystemExit(f"{addon_id} needs {dep}, which is not in {args.repo}")
                version = index[dep].get("version")
                extract_single_dir(fetch(f"{args.repo}/{dep}/{dep}-{version}.zip"), dep_dir)
                changed = True
            visit(dep, ET.parse(dep_dir / "addon.xml").getroot())
            order.append(dep)

    visit(PM4K_ID, ET.parse(pm4k / "addon.xml").getroot())
    order.append(PM4K_ID)
    return {"changed": changed, "order": order}


def rpc(url: str, method: str, params: dict | None = None) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def enable(args: argparse.Namespace) -> dict:
    deadline = time.monotonic() + args.timeout
    while True:
        try:
            rpc(args.jsonrpc, "JSONRPC.Ping")
            break
        except OSError:
            if time.monotonic() > deadline:
                raise SystemExit(f"Kodi JSON-RPC not reachable at {args.jsonrpc}")
            time.sleep(2)

    order = install_order(Path(args.addons_dir).expanduser()) + list(args.extra)
    changed = False
    for addon_id in order:
        details = rpc(args.jsonrpc, "Addons.GetAddonDetails", {"addonid": addon_id, "properties": ["enabled"]})
        if details.get("result", {}).get("addon", {}).get("enabled"):
            continue
        result = rpc(args.jsonrpc, "Addons.SetAddonEnabled", {"addonid": addon_id, "enabled": True})
        if result.get("result") != "OK":
            raise SystemExit(f"could not enable {addon_id}: {result}")
        changed = True
    return {"changed": changed, "order": order}


def install_order(addons: Path) -> list[str]:
    order: list[str] = []

    def visit(addon_id: str) -> None:
        xml = ET.parse(addons / addon_id / "addon.xml").getroot()
        for dep in requires(xml):
            if dep not in order:
                visit(dep)
        if addon_id not in order:
            order.append(addon_id)

    visit(PM4K_ID)
    return order


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_install = sub.add_parser("install")
    p_install.add_argument("--addons-dir", required=True)
    p_install.add_argument("--repo", required=True)
    p_install.add_argument("--pm4k-ref", required=True)
    p_enable = sub.add_parser("enable")
    p_enable.add_argument("--addons-dir", required=True)
    p_enable.add_argument("--jsonrpc", required=True)
    p_enable.add_argument("--extra", nargs="*", default=[])
    p_enable.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    result = install(args) if args.cmd == "install" else enable(args)
    json.dump(result, sys.stdout)
    print()


if __name__ == "__main__":
    main()
