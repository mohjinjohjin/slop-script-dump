#!/usr/bin/env python3
"""
Set the "Physics Type" of every NON-vanilla entity in a Physics Mod
mobs config (physics_mobs_client_config.json) to 4.

Vanilla = identifier in the "minecraft:" namespace; those are never touched.

Usage:
    python set_modded_physics_type.py [path/to/physics_mobs_client_config.json]
    python set_modded_physics_type.py config.json --type 4 --dry-run
    python set_modded_physics_type.py config.json -o fixed.json     (leave the original alone)

With no path it looks for physics_mobs_client_config.json in the current
folder, or you can drag the file onto this script.
Without -o the file is edited in place and the original is saved as <file>.bak.
Everything else in the file (field order, 2-space indent, 100.0 floats) is kept.
"""
import argparse
import json
import os
import shutil
import sys
from collections import Counter

DEFAULT_NAME = "physics_mobs_client_config.json"
VANILLA_NAMESPACE = "minecraft"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", default=DEFAULT_NAME)
    ap.add_argument("--type", type=int, default=4, help="Physics Type to set (default 4)")
    ap.add_argument("-o", "--output", help="write here instead of editing the file in place")
    ap.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    args = ap.parse_args()

    if not os.path.isfile(args.path):
        sys.exit(f"Can't find '{args.path}'. Pass the path to the config file.")

    with open(args.path, "r", encoding="utf-8-sig", newline="") as fh:
        raw = fh.read()
    try:
        data = json.loads(raw)
        mobs = data["customizedMobs"]
    except (ValueError, KeyError, TypeError) as e:
        sys.exit(f"This doesn't look like a Physics Mod mobs config ({e}).")

    changed, already, vanilla = [], 0, 0
    for mob in mobs:
        ident = str(mob.get("identifier", ""))
        if ident.split(":", 1)[0] == VANILLA_NAMESPACE or ":" not in ident:
            vanilla += 1                      # vanilla (or malformed) - leave alone
            continue
        old = mob.get("Physics Type")
        if old == args.type:
            already += 1
            continue
        changed.append((ident, old))
        mob["Physics Type"] = args.type

    print(f"{len(mobs)} entries: {vanilla} vanilla (untouched), "
          f"{already} modded already {args.type}, {len(changed)} modded changed to {args.type}.")
    for ident, old in changed:
        print(f"  {ident}: {old} -> {args.type}")
    if not changed:
        print("Nothing to do.")
        return
    if args.dry_run:
        print("(dry run - nothing written)")
        return

    out = json.dumps(data, indent=2, ensure_ascii=False)
    if raw.endswith("\n"):
        out += "\n"
    target = args.output or args.path
    if not args.output:
        shutil.copy2(args.path, args.path + ".bak")
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(out)
    print(f"Saved {target}" + ("" if args.output else f"  (backup: {args.path}.bak)"))


if __name__ == "__main__":
    main()
