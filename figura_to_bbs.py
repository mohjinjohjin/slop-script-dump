#!/usr/bin/env python3
"""
Figura .bbmodel  ->  BBS mod model (.bbs.json + texture .png)

Keeps the whole bone hierarchy (every Blockbench group becomes a BBS group with the same name,
pivot and parent) and every cube, so the bones can be posed in BBS.

Writes a model folder: <id>.bbs.json, <id>.png, config.json and poses.json.

By default the groups are fitted to BBS's player rig (the one in steve.bbs.json), because config.json and
poses.json refer to bones by those names and parents (anchor > low_body > body > torso/arms, low_body > head,
anchor > legs). Figura's Head/Body/LeftArm/... groups are renamed head/torso/left_arm/..., its vanilla pivots
(HelmetPivot, LeftItemPivot, ...) become the armor_*/item groups, and anything else keeps its name and moves with
its parent. Rest poses are unchanged. Use --keep-names to skip all of that and write only the model + texture.

Usage:
    python figura_to_bbs.py model.bbmodel                  # writes the folder ./model/ next to the input
    python figura_to_bbs.py model.bbmodel -o out/mymodel   # writes the folder out/mymodel/

Options:
    --keep-names      don't rename/reparent to the BBS player rig (no config.json / poses.json then)
    --poses FILE      poses.json to copy from (default: poses.json next to this script)
    --texture-path P  value for config.json "texture" (default assets:models/<id>/<id>.png)
    --keep-hidden     also convert cubes/groups hidden in Blockbench (skipped by default: Figura scripts
                      usually toggle them, e.g. an alternate face, and BBS would draw them all at once)
    --rot-key NAME    JSON key used for rotations (default "rotate")

Needs only Python 3.8+. The format was read from BBS's own steve.bbs.json (version 0.7.2).
"""
import argparse
import base64
import json
import os
import sys

FACE_MAP = {"north": "front", "south": "back", "east": "right", "west": "left", "down": "bottom", "up": "top"}


def num(v):
    v = round(float(v), 5)
    return int(v) if v == int(v) else v


def vec(v, default=(0, 0, 0)):
    v = v if isinstance(v, (list, tuple)) and len(v) == 3 else default
    return [num(x) for x in v]


def convert(data, keep_hidden=False, rot_key="rotate"):
    warnings = []
    elements = {e["uuid"]: e for e in data.get("elements", []) if isinstance(e, dict) and e.get("uuid")}
    group_defs = {g["uuid"]: g for g in (data.get("groups") or []) if isinstance(g, dict) and g.get("uuid")}
    groups, used = {}, set()

    def unique(name):
        base, n = name or "group", 2
        name = base
        while name in used:
            name, n = f"{base}_{n}", n + 1
        used.add(name)
        return name

    def convert_cube(el):
        if el.get("type", "cube") != "cube":
            warnings.append(f"skipped '{el.get('name')}': {el.get('type')} elements aren't supported")
            return None
        if el.get("visibility") is False and not keep_hidden:
            return None
        f, t = el.get("from"), el.get("to")
        if not (isinstance(f, list) and isinstance(t, list) and len(f) == 3 and len(t) == 3):
            return None
        cube = {"origin": vec(el.get("origin")), "from": vec(f), "size": [num(t[i] - f[i]) for i in range(3)]}
        if any(el.get("rotation") or []):
            cube[rot_key] = vec(el.get("rotation"))
        if el.get("inflate"):
            cube["offset"] = num(el["inflate"])
        uvs = {}
        for face, key in FACE_MAP.items():
            fd = (el.get("faces") or {}).get(face)
            if not isinstance(fd, dict) or fd.get("texture") is None or fd.get("texture") is False:
                continue
            uv = fd.get("uv")
            if isinstance(uv, list) and len(uv) == 4:
                uvs[key] = [num(x) for x in uv]
            if fd.get("rotation"):
                warnings.append(f"'{el.get('name')}' face {face}: texture rotation {fd['rotation']} is not converted")
        cube["uvs"] = uvs
        return cube

    def walk(node, parent):
        if isinstance(node, str):                       # a cube; cubes outside any group go into 'anchor'
            el = elements.get(node)
            if el is None:
                return
            cube = convert_cube(el)
            if cube is None:
                return
            if parent is None:
                if "anchor" not in groups:
                    groups["anchor"] = {"origin": [0, 0, 0]}
                    used.add("anchor")
                parent = "anchor"
            groups[parent].setdefault("cubes", []).append(cube)
            return
        if not isinstance(node, dict):
            return
        g = {**group_defs.get(node.get("uuid"), {}), **node}     # newer files keep name/origin in "groups"
        if g.get("visibility") is False and not keep_hidden:
            return
        name = unique(str(g.get("name", "")))
        out = {"origin": vec(g.get("origin"))}
        if parent:
            out["parent"] = parent
        if any(g.get("rotation") or []):
            out[rot_key] = vec(g.get("rotation"))
        groups[name] = out
        for ch in node.get("children", []) or []:
            walk(ch, name)

    for item in data.get("outliner", []) or []:
        walk(item, None)

    # BBS lists children before parents in steve.bbs.json, but order is only cosmetic: parents are by name.
    res = data.get("resolution") if isinstance(data.get("resolution"), dict) else {}
    tex = [int(res.get("width", 64)), int(res.get("height", 64))]
    return {"version": "0.7.2", "animations": {}, "model": {"groups": groups, "texture": tex}}, warnings


# ---- BBS player rig ------------------------------------------------------------------------------------------
RIG_SKELETON = {                     # name: (origin, parent), from steve.bbs.json
    "anchor": ([0, 16, 0], None),
    "low_body": ([0, 12, 0], "anchor"),
    "body": ([0, 24, 0], "low_body"),
}
RIG_PARENT = {"torso": "body", "left_arm": "body", "right_arm": "body", "head": "low_body",
              "left_leg": "anchor", "right_leg": "anchor"}
RIG_NAMES = {                        # normalised Figura name -> BBS name
    "head": "head", "body": "torso", "leftarm": "left_arm", "rightarm": "right_arm",
    "leftleg": "left_leg", "rightleg": "right_leg",
    "helmetpivot": "armor_helmet", "chestplatepivot": "armor_chest", "leggingspivot": "armor_leggings",
    "leftshoulderpivot": "armor_left_arm", "rightshoulderpivot": "armor_right_arm",
    "leftleggingpivot": "armor_left_leg", "rightleggingpivot": "armor_right_leg",
    "leftbootpivot": "armor_left_boot", "rightbootpivot": "armor_right_boot",
    "leftitempivot": "left_arm_item", "rightitempivot": "right_arm_item",
}


def apply_player_rig(groups):
    """Rename/reparent Figura's vanilla groups onto the BBS rig. Returns a list of notes."""
    notes = []
    norm = lambda n: n.lower().replace("_", "").replace(" ", "")
    rename = {}
    for name in groups:
        new = RIG_NAMES.get(norm(name))
        if new and new != name and new not in groups:
            rename[name] = new
    # a group named 'root' that holds everything is just Figura's container: dissolve it
    for name, g in list(groups.items()):
        if norm(name) == "root" and not g.get("parent"):
            kids = [k for k, v in groups.items() if v.get("parent") == name]
            if kids and name not in rename:
                for k in kids:
                    groups[k].pop("parent", None)
                if g.get("cubes"):
                    groups.setdefault("anchor", {"origin": [0, 16, 0]}).setdefault("cubes", []).extend(g["cubes"])
                if any(key not in ("origin", "parent", "cubes") for key in g):
                    notes.append(f"'{name}' had a rotation; it was dropped when the group was dissolved")
                del groups[name]
    out = {}
    for name, g in groups.items():
        g = dict(g)
        if g.get("parent") in rename:
            g["parent"] = rename[g["parent"]]
        out[rename.get(name, name)] = g
    groups.clear()
    groups.update(out)
    for name, (origin, parent) in RIG_SKELETON.items():
        if name not in groups:
            groups[name] = {"origin": list(origin)}
        elif parent:
            groups[name]["parent"] = parent
        if parent:
            groups[name]["parent"] = parent
    for name, parent in RIG_PARENT.items():
        if name in groups:
            groups[name]["parent"] = parent
    for name, g in groups.items():           # everything else without a parent hangs off the anchor
        if not g.get("parent") and name != "anchor":
            g["parent"] = "anchor"
    # keep the dict ordered like BBS expects to read it: parents are by name, so order is cosmetic
    if rename:
        notes.append("renamed: " + ", ".join(f"{a} -> {b}" for a, b in rename.items()))
    return notes


STEVE_CONFIG = {
    "texture": "assets:models/player/steve/steve.png", "procedural": True, "culling": False,
    "items_main": ["right_arm_item"], "items_off": ["left_arm_item"], "scale": [0.9375, 0.9375, 0.9375],
    "look_at": {"head": "head", "pitch": True, "head_limit": 45},
    "armor_slots": {"helmet": {"group": "armor_helmet"}, "chest": {"group": "armor_chest"},
                    "leggings": {"group": "armor_leggings"}, "left_arm": {"group": "armor_left_arm"},
                    "right_arm": {"group": "armor_right_arm"}, "left_leg": {"group": "armor_left_leg"},
                    "right_leg": {"group": "armor_right_leg"}, "left_boot": {"group": "armor_left_boot"},
                    "right_boot": {"group": "armor_right_boot"}},
    "sneaking_pose": {"static": False, "pose": {
        "head": {"r2": [0.0, 0.0, 0.0], "r": [0.0, 0.0, 0.0], "s": [1.0, 1.0, 1.0], "t": [0.0, -4.2, 0.0], "fix": 0.0},
        "left_leg": {"r2": [0.0, 0.0, 0.0], "r": [0.0, 0.0, 0.0], "s": [1.0, 1.0, 1.0], "t": [0.0, -0.2, 4.0], "fix": 0.0},
        "right_arm": {"r2": [0.0, 0.0, 0.0], "r": [-0.39999452, 0.0, 0.0], "s": [1.0, 1.0, 1.0], "t": [0.0, -3.2, 0.0], "fix": 0.0},
        "torso": {"r2": [0.0, 0.0, 0.0], "r": [-0.50000197, 0.0, 0.0], "s": [1.0, 1.0, 1.0], "t": [0.0, -3.2, 0.0], "fix": 0.0},
        "right_leg": {"r2": [0.0, 0.0, 0.0], "r": [0.0, 0.0, 0.0], "s": [1.0, 1.0, 1.0], "t": [0.0, -0.2, 4.0], "fix": 0.0},
        "left_arm": {"r2": [0.0, 0.0, 0.0], "r": [-0.39999452, 0.0, 0.0], "s": [1.0, 1.0, 1.0], "t": [0.0, -3.2, 0.0], "fix": 0.0}}},
    "fp_main": {"group": "right_arm", "transform": {"t": [0.13875, 1.525, 0], "r": [180, 0, -5.729]}},
    "fp_offhand": {"group": "left_arm", "transform": {"t": [-0.13875, 1.525, 0], "r": [180, 0, 5.729]}},
}


def make_config(groups, texture_path):
    """Steve's config.json, keeping only the entries whose groups exist in this model."""
    c = json.loads(json.dumps(STEVE_CONFIG))
    c["texture"] = texture_path
    c["items_main"] = [g for g in c["items_main"] if g in groups]
    c["items_off"] = [g for g in c["items_off"] if g in groups]
    if c["look_at"]["head"] not in groups:
        del c["look_at"]
    c["armor_slots"] = {k: v for k, v in c["armor_slots"].items() if v["group"] in groups}
    c["sneaking_pose"]["pose"] = {k: v for k, v in c["sneaking_pose"]["pose"].items() if k in groups}
    for key in ("fp_main", "fp_offhand"):
        if c[key]["group"] not in groups:
            del c[key]
    return c


def make_poses(path, groups):
    """The poses library with bones this model doesn't have removed (and poses left empty dropped)."""
    with open(path, "r", encoding="utf-8-sig") as fh:
        poses = json.load(fh)
    out, dropped = {}, set()
    for name, p in poses.items():
        bones = {b: v for b, v in (p.get("pose") or {}).items() if b in groups}
        dropped |= {b for b in (p.get("pose") or {}) if b not in groups}
        if bones:
            out[name] = {**p, "pose": bones}
    return out, sorted(dropped)


def extract_texture(data, png_path):
    for t in data.get("textures", []) or []:
        src = t.get("source") if isinstance(t, dict) else None
        if isinstance(src, str) and src.startswith("data:") and "," in src:
            with open(png_path, "wb") as fh:
                fh.write(base64.b64decode(src.split(",", 1)[1]))
            return t.get("name") or True
    return None


def main():
    ap = argparse.ArgumentParser(description="Convert a Figura .bbmodel into a BBS mod model folder.")
    ap.add_argument("model")
    ap.add_argument("-o", "--output", help="output folder (default: a folder named after the model, next to it)")
    ap.add_argument("--keep-hidden", action="store_true")
    ap.add_argument("--rot-key", default="rotate")
    ap.add_argument("--keep-names", action="store_true")
    ap.add_argument("--poses", help="poses.json to copy from (default: poses.json next to this script)")
    ap.add_argument("--texture-path", help='config.json "texture" value')
    a = ap.parse_args()

    with open(a.model, "r", encoding="utf-8-sig") as fh:
        data = json.load(fh)
    out, warnings = convert(data, a.keep_hidden, a.rot_key)
    groups = out["model"]["groups"]
    if not a.keep_names:
        for n in apply_player_rig(groups):
            print("note:", n)
    mid = os.path.splitext(os.path.basename(a.model))[0]
    folder = a.output or os.path.join(os.path.dirname(os.path.abspath(a.model)), mid)
    mid = os.path.basename(os.path.normpath(folder)) or mid
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, mid + ".bbs.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent="\t")
    tex = extract_texture(data, os.path.join(folder, mid + ".png"))
    cubes = sum(len(g.get("cubes", [])) for g in groups.values())
    print(f"Wrote {folder}/{mid}.bbs.json: {len(groups)} groups, {cubes} cubes, texture {out['model']['texture']}")
    print(f"Wrote {mid}.png" if tex else "No embedded texture found: put your skin PNG in the folder yourself.")
    if a.keep_names:
        print("Skipped config.json and poses.json (they need the BBS rig names; drop --keep-names to get them).")
    else:
        cfg = make_config(groups, a.texture_path or f"assets:models/{mid}/{mid}.png")
        with open(os.path.join(folder, "config.json"), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent="\t")
        print("Wrote config.json")
        src = a.poses or os.path.join(os.path.dirname(os.path.abspath(__file__)), "poses.json")
        if os.path.isfile(src):
            poses, dropped = make_poses(src, groups)
            with open(os.path.join(folder, "poses.json"), "w", encoding="utf-8") as fh:
                json.dump(poses, fh, indent="\t")
            print(f"Wrote poses.json: {len(poses)} poses" + (f" (bones this model lacks were left out: {', '.join(dropped)})" if dropped else ""))
        else:
            print("No poses.json found next to the script: pass --poses FILE (e.g. Steve's) to get one.")
    for w in warnings:
        print("warning:", w, file=sys.stderr)


if __name__ == "__main__":
    main()
