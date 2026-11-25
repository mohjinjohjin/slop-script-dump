#!/usr/bin/env python3
"""
Figura PhysBone Editor
======================
A slider-based editor for the parameters of Figura physBones
(https://github.com/ChloeSpacedOut/figura-physbone-api).

It reads your avatar's Lua script (script.lua), finds every
    physBone.<bone>:setGravity(-4)          physBone["<bone>"]:setLength(5)
    local x = physBone.<bone>   x:setMass(2)   models.a.b:getPhysBone():setBounce(.4)
call (chains and aliases included), and gives each bone sliders for EVERY
parameter. Saving edits the script in place: existing calls are changed where
they are, new calls are added at the end of events.entity_init() (or right after
that bone's other calls). Nothing else in your script is touched.

Live preview: load your avatar's .bbmodel and watch the physBones move with the editor's current
values. The physics is a port of physBoneAPI.lua's own algorithm (collisions excluded), textured,
with walking / start-stop / jump motions and drag-to-throw. It draws with numpy + Pillow when
available and installs them itself (into ~/.figura_physbone_editor/libs, never the system Python) the
first time you open the preview; if that isn't possible it quietly uses a slower built-in renderer. Bones start from their preset
(physBone / physBoob / your setPreset()s), exactly like in-game.

Presets: save a bone's settings under a name, apply them to one or many bones,
import/export them as .json files, or copy them as a physBone:setPreset(...) line.
setPreset() calls already in your script show up as "[script] ..." presets.

Usage:   python figura_physbone_editor.py [path/to/script.lua]
Needs Python 3.8+ and tkinter (offered automatically if missing).
"""
import base64
import copy
import difflib
import importlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import zlib
from collections import OrderedDict


def ensure_dependencies():
    """tkinter is the only dependency; pip can't install it, so use the OS package manager."""
    try:
        import tkinter  # noqa: F401
        return
    except ImportError:
        pass
    ver = f"{sys.version_info.major}.{sys.version_info.minor}"
    cmd = None
    if sys.platform.startswith("linux"):
        table = [("apt-get", ["apt-get", "install", "-y", "python3-tk"]),
                 ("dnf", ["dnf", "install", "-y", "python3-tkinter"]),
                 ("pacman", ["pacman", "-S", "--noconfirm", "tk"]),
                 ("zypper", ["zypper", "--non-interactive", "install", "python3-tk"]),
                 ("apk", ["apk", "add", "py3-tkinter"])]
        for exe, c in table:
            if shutil.which(exe):
                cmd = c if (hasattr(os, "geteuid") and os.geteuid() == 0) else ["sudo"] + c
                break
    elif sys.platform == "darwin" and shutil.which("brew"):
        cmd = ["brew", "install", f"python-tk@{ver}"]
    print("This tool needs tkinter (Python's built-in GUI library), which isn't installed.")
    if cmd is None:
        if sys.platform.startswith("win"):
            print("Windows: re-run the Python installer, choose Modify, and tick 'tcl/tk and IDLE'.")
        else:
            print("Please install tkinter for your Python (package is usually python3-tk) and run again.")
        sys.exit(1)
    print("I can install it with:  " + " ".join(cmd))
    if sys.stdin and sys.stdin.isatty() and input("Run that now? [Y/n] ").strip().lower() in ("n", "no"):
        sys.exit(1)
    try:
        subprocess.check_call(cmd)
    except (OSError, subprocess.CalledProcessError) as e:
        print(f"Install failed ({e}). Install tkinter manually and try again.")
        sys.exit(1)
    importlib.invalidate_caches()
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("tkinter still isn't available to this Python (virtualenv?). Use the system Python.")
        sys.exit(1)


if __name__ == "__main__":
    ensure_dependencies()

import tkinter as tk  # noqa: E402
from tkinter import filedialog, messagebox, simpledialog, ttk  # noqa: E402
from tkinter import font as tkfont  # noqa: E402

SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".figura_physbone_editor.json")
DEFAULT_PRESET_FILE = os.path.join(os.path.expanduser("~"), ".figura_physbone_presets.json")


# ==========================================================================
# Parameters (from the physBone API wiki: Documentation > PhysBone)
# ==========================================================================
class Param:
    def __init__(self, key, label, kind, default, lo, hi, step, group, hint=""):
        self.key, self.label, self.kind = key, label, kind          # kind: "num" | "vec3"
        self.default, self.lo, self.hi, self.step = default, lo, hi, step
        self.group, self.hint = group, hint
        self.setter = "set" + key[0].upper() + key[1:]
        self.integer = step >= 1 and key == "nodeDensity"


PARAMS = [
    Param("mass", "Mass", "num", 1, 0.05, 10, 0.05, "Physics", "Weight of the bone. Must be above 0. Default 1."),
    Param("length", "Length", "num", 16, 0, 64, 0.5, "Physics", "Length in Blockbench units. Default 16."),
    Param("gravity", "Gravity", "num", -9.81, -30, 30, 0.01, "Physics", "Strength of gravity. Default -9.81."),
    Param("airResistance", "Air resistance", "num", 0.1, 0, 2, 0.01, "Physics", "Damping from the air. Default 0.1."),
    Param("simSpeed", "Simulation speed", "num", 1, 0, 4, 0.05, "Physics", "Speed of the simulation. Default 1."),
    Param("bounce", "Bounce", "num", 0.75, 0, 1, 0.01, "Physics", "Collision bounciness (0-1). Default 0.75."),
    Param("springForce", "Spring force", "num", 0, 0, 200, 1, "Spring", "Strength of the spring pull. Default 0."),
    Param("equilibrium", "Equilibrium", "vec3", [0, -1, 0], -1, 1, 0.01, "Spring",
          "Direction the spring pulls toward (normalized). Default (0,-1,0)."),
    Param("force", "Constant force", "vec3", [0, 0, 0], -100, 100, 0.5, "Forces", "Force applied constantly. Default (0,0,0)."),
    Param("vecMod", "Rotation constraint", "vec3", [1, 1, 1], 0, 1, 0.01, "Rotation constraints",
          "How freely the bone swings on each axis (vecMod): 1 = free, 0 = locked, in between = restricted. "
          "Default (1,1,1)."),
    Param("rotMod", "Rotation modifier", "vec3", [0, 0, 0], -180, 180, 1, "Model alignment",
          "Fixes models whose parts don't match the bone direction (applied ZYX). Default (0,0,0)."),
    Param("rollMod", "Roll modifier", "num", 0, -180, 180, 1, "Model alignment", "Rolls the model around Y, degrees. Default 0."),
    Param("nodeStart", "Node start", "num", 0, -16, 64, 0.5, "Collision nodes", "Where node generation starts (BB units, can be negative). Default 0."),
    Param("nodeEnd", "Node end", "num", 16, -16, 64, 0.5, "Collision nodes", "Where node generation ends (BB units). Default 16."),
    Param("nodeDensity", "Node density", "num", 1, 0, 16, 1, "Collision nodes", "Number of nodes; 0 turns off all collider interaction. Default 1."),
    Param("nodeRadius", "Node radius", "num", 0, 0, 16, 0.1, "Collision nodes", "Collision radius of each node. Default 0."),
]
PARAM_BY_KEY = {p.key: p for p in PARAMS}
SETTERS = {p.setter: p for p in PARAMS}
GROUPS = ["Physics", "Spring", "Forces", "Rotation constraints", "Model alignment", "Collision nodes"]
# positional order of physBone:setPreset(id, ...)
PRESET_ORDER = ["mass", "length", "gravity", "airResistance", "simSpeed", "equilibrium", "springForce", "force",
                "rotMod", "vecMod", "rollMod", "nodeStart", "nodeEnd", "nodeDensity", "nodeRadius", "bounce"]
BONE_PREFIXES = ("physBone", "PhysBone", "physBoob", "PhysBoob")


def default_value(p):
    return list(p.default) if p.kind == "vec3" else p.default


def _api_preset(**kw):
    d = {p.key: default_value(p) for p in PARAMS}
    d.update(copy.deepcopy(kw))
    return d


# The presets that ship inside physBoneAPI.lua. A bone created with newPhysBone("physBoob") (or a group whose
# name starts with it) starts from these values; anything the script sets afterwards overrides them.
_BOOB = dict(mass=2.0, airResistance=0.5, equilibrium=[0.0, 0.0, -1.0], springForce=200.0,
             rotMod=[-90.0, 0.0, 0.0], nodeDensity=0.0)
API_PRESETS = OrderedDict([("physBone", _api_preset()), ("PhysBone", _api_preset()),
                           ("physBoob", _api_preset(**_BOOB)), ("PhysBoob", _api_preset(**_BOOB))])
BUILTIN_PRESETS = OrderedDict([("physBone", API_PRESETS["physBone"]), ("physBoob", API_PRESETS["physBoob"])])


def same(a, b):
    if a is None or b is None:
        return a is b
    if isinstance(a, (list, tuple)) or isinstance(b, (list, tuple)):
        return (isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)) and len(a) == len(b)
                and all(abs(x - y) < 5e-5 for x, y in zip(a, b)))
    return abs(a - b) < 5e-5


def fmt_num(v):
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def fmt_value(p, v, ctor="vec"):
    if p.kind == "vec3":
        return f"{ctor}({fmt_num(v[0])}, {fmt_num(v[1])}, {fmt_num(v[2])})"
    return fmt_num(v)


# ==========================================================================
# Lua helpers (no full parser: just enough to find calls safely)
# ==========================================================================
def lua_mask(text, keep_strings):
    """Same-length copy of text with comments (and, optionally, string contents) blanked."""
    out = list(text)
    n, i = len(text), 0

    def blank(a, b):
        for k in range(a, min(b, n)):
            if out[k] != "\n":
                out[k] = " "
    while i < n:
        c = text[i]
        if c == "-" and text.startswith("--", i):
            m = re.match(r"--\[(=*)\[", text[i:i + 40])
            if m:
                close = "]" + m.group(1) + "]"
                e = text.find(close, i + len(m.group(0)))
                e = n if e < 0 else e + len(close)
            else:
                e = text.find("\n", i)
                e = n if e < 0 else e
            blank(i, e)
            i = e
        elif c in "\"'":
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            e = min(j + 1, n)
            if not keep_strings:
                blank(i + 1, e - 1)
            i = e
        elif c == "[":
            m = re.match(r"\[(=*)\[", text[i:i + 40])
            if m:
                close = "]" + m.group(1) + "]"
                e = text.find(close, i + len(m.group(0)))
                e = n if e < 0 else e + len(close)
                if not keep_strings:
                    blank(i, e)
                i = e
            else:
                i += 1
        else:
            i += 1
    return "".join(out)


def match_paren(code, open_i):
    """Index of the ')' matching code[open_i] == '(' (strings kept in `code`), or None."""
    depth, i, n = 0, open_i, len(code)
    while i < n:
        c = code[i]
        if c in "\"'":
            j = i + 1
            while j < n and code[j] != c and code[j] != "\n":
                j += 2 if code[j] == "\\" else 1
            i = j
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def split_args(s):
    """Split on top-level commas (respects brackets and strings)."""
    parts, depth, cur, i, n = [], 0, [], 0, len(s)
    while i < n:
        c = s[i]
        if c in "\"'":
            j = i + 1
            while j < n and s[j] != c:
                j += 2 if s[j] == "\\" else 1
            cur.append(s[i:j + 1])
            i = j + 1
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        if c == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    if "".join(cur).strip() or parts:
        parts.append("".join(cur))
    return [p.strip() for p in parts]


NUM_RE = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")
VEC_RE = re.compile(r"^(vec3?|vectors\.vec3?)\s*\((.*)\)$", re.S)
UNPARSED = object()


def parse_value(arg, kind):
    s = arg.strip()
    if kind == "num":
        return float(s) if NUM_RE.match(s) else UNPARSED
    m = VEC_RE.match(s)
    if not m:
        return UNPARSED
    parts = split_args(m.group(2))
    if len(parts) == 3 and all(NUM_RE.match(x) for x in parts):
        return [float(x) for x in parts]
    return UNPARSED


def find_matching_end(scode, pos):
    """Index of the 'end' that closes a block whose body starts at `pos` (strings/comments blanked)."""
    depth, skip_do = 1, False
    for m in re.finditer(r"(?<![\w.])(function|if|for|while|do|repeat|end|until)\b", scode[pos:]):
        t = m.group(1)
        if t in ("for", "while"):
            depth += 1
            skip_do = True
        elif t == "do":
            if skip_do:
                skip_do = False
            else:
                depth += 1
        elif t in ("function", "if", "repeat"):
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                return pos + m.start()
    return None


def lua_str(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def lua_accessor(pb_var, name):
    return f"{pb_var}.{name}" if re.match(r"^[A-Za-z_]\w*$", name) else f"{pb_var}[{lua_str(name)}]"


# ==========================================================================
# Script model
# ==========================================================================
class Call:
    pass


class Bone:
    def __init__(self, name, extra=False):
        self.name = name
        self.extra = extra               # added by hand / from a model, no calls in the script
        self.orig = {}                   # param -> value read from the script
        self.calls = {}                  # param -> effective Call
        self.unparsed = {}               # param -> raw argument text (expression we can't read)
        self.values = {}                 # param -> value edited in the UI
        self.touched = set()
        self.orig_colliders, self.colliders = [], []
        self.collider_calls = {}
        self.accessor = None
        self.after = None                # (insert_pos, indent, need_newline) after the bone's last statement
        self.preset_id = None            # preset the bone was created with, e.g. "physBoob"
        self.base_vals = {}              # that preset's values (what the bone has before the script's own calls)

    def base(self, key):
        v = self.base_vals.get(key)
        return copy.deepcopy(v) if v is not None else default_value(PARAM_BY_KEY[key])

    def get(self, key):
        if key in self.values:
            return copy.deepcopy(self.values[key])
        if key in self.orig:
            return copy.deepcopy(self.orig[key])
        return self.base(key)

    def set(self, key, value):
        self.values[key] = copy.deepcopy(value)
        self.touched.add(key)

    def state(self, key):
        """'edited' | 'script' | 'expr' | 'preset' | 'default' - where the shown value comes from."""
        cur = self.get(key)
        if key in self.touched and not (key in self.orig and same(cur, self.orig[key])) \
                and not (key not in self.calls and same(cur, self.base(key))):
            return "edited"
        if key in self.unparsed:
            return "expr"
        if key in self.calls:
            return "script"
        if key in self.base_vals and not same(self.base_vals[key], default_value(PARAM_BY_KEY[key])):
            return "preset"
        return "default"

    def dirty(self):
        return any(self.state(k) == "edited" for k in self.touched) or \
            sorted(self.colliders) != sorted(self.orig_colliders)

    def script_count(self):
        return len(self.calls)


REQUIRE_RE = re.compile(r"(?:local\s+)?([A-Za-z_]\w*)\s*=\s*require\s*\(?\s*['\"]([^'\"]*physBoneAPI)['\"]\s*\)?")
INIT_RES = [re.compile(r"function\s+events\s*\.\s*(?:entity_init|ENTITY_INIT)\s*\(\s*\)"),
            re.compile(r"events\s*\.\s*(?:entity_init|ENTITY_INIT)\s*:\s*register\s*\(\s*function\s*\(\s*\)")]
CHAIN_RE = re.compile(r"\s*:\s*([A-Za-z_]\w*)\s*\(")


def parse_chain(code, pos):
    calls, i = [], pos
    while True:
        m = CHAIN_RE.match(code, i)
        if not m:
            break
        open_i = m.end() - 1
        close_i = match_paren(code, open_i)
        if close_i is None:
            break
        c = Call()
        c.method, c.colon, c.name_start, c.open, c.close = m.group(1), code.index(":", i, m.end()), m.start(1), open_i, close_i
        calls.append(c)
        i = close_i + 1
    return calls


class ScriptDoc:
    def __init__(self, path=None, text="", extra_names=()):
        self.path, self.mtime = path, None
        self.script_presets = OrderedDict()   # id -> {param: value}
        self.extra_names = list(extra_names)
        self.parse(text)

    def _real(self, start, token):
        """True if `token` at `start` is code, not text inside a string literal."""
        return self.scode[start:start + len(token)] == token

    # ----- parsing -----------------------------------------------------------
    def parse(self, text):
        self.text = text
        code = self.code = lua_mask(text, True)
        scode = self.scode = lua_mask(text, False)
        self.bones = OrderedDict()
        self.script_presets = OrderedDict()
        m = REQUIRE_RE.search(code)
        self.pb_var = m.group(1) if m else "physBone"
        self.has_require = bool(m)
        pb = re.escape(self.pb_var)

        aliases = {}
        for m in re.finditer(rf'(?<![\w.:])(?:local\s+)?([A-Za-z_]\w*)\s*=\s*{pb}\s*(?:\.\s*([A-Za-z_]\w*)|\[\s*(["\'])(.*?)\3\s*\])[ \t]*(?=;|\n|$)',
                             code, re.M):
            if m.group(1) != self.pb_var and self._real(m.start(1), m.group(1)):
                aliases[m.group(1)] = m.group(2) or m.group(4)
        for m in re.finditer(r'(?<![\w.:])(?:local\s+)?([A-Za-z_]\w*)\s*=\s*([A-Za-z_][\w.]*)\s*:\s*(?:getPhysBone|newPhysBone)\s*\([^()]*\)[ \t]*(?=;|\n|$)',
                             code, re.M):
            if self._real(m.start(1), m.group(1)):
                aliases[m.group(1)] = m.group(2).split(".")[-1]

        chains = []   # (bone, acc_start, acc_end, calls, preset id | None, creates the bone?)
        for m in re.finditer(rf'(?<![\w.:]){pb}\s*(?:\.\s*([A-Za-z_]\w*)|\[\s*(["\'])(.*?)\2\s*\])', code):
            calls = parse_chain(code, m.end()) if self._real(m.start(), self.pb_var) else None
            if calls:
                chains.append((m.group(1) or m.group(3), m.start(), m.end(), calls, None, False))
        for alias, bone in aliases.items():
            for m in re.finditer(rf'(?<![\w.:]){re.escape(alias)}(?=\s*:)', code):
                calls = parse_chain(code, m.end()) if self._real(m.start(), alias) else None
                if calls:
                    chains.append((bone, m.start(), m.end(), calls, None, False))
        for m in re.finditer(r'(?<![\w.:])([A-Za-z_][\w.]*)\s*:\s*(?:getPhysBone|newPhysBone)\s*\(([^()]*)\)', code):
            calls = parse_chain(code, m.end()) if self._real(m.start(1), m.group(1)) else None
            if calls:
                pm = re.match(r'\s*(["\'])(.*?)\1', m.group(2))
                chains.append((m.group(1).split(".")[-1], m.start(), m.end(), calls, pm.group(2) if pm else None, True))
        chains.sort(key=lambda c: c[1])

        for bone_name, a_start, a_end, calls, pid, creates in chains:
            bone = self.bones.setdefault(bone_name, Bone(bone_name))
            if pid and bone.preset_id is None:
                bone.preset_id = pid
            # new calls must refer to the EXISTING bone (physBone.name), never repeat the newPhysBone(...) expression
            acc = lua_accessor(self.pb_var, bone_name) if creates else code[a_start:a_end].strip()
            if bone.accessor is None:
                bone.accessor = acc
            chain_end = calls[-1].close
            ls = code.rfind("\n", 0, a_start) + 1
            le = code.find("\n", chain_end)
            before = code[ls:a_start]
            after = code[chain_end + 1:(len(code) if le < 0 else le)]
            tail_ok = after.strip() in ("", ";")
            alone = before.strip() == "" and tail_ok
            assign = bool(re.fullmatch(r"[ \t]*(?:local\s+)?[A-Za-z_][\w.]*\s*=\s*", before)) and tail_ok   # `Name = part:newPhysBone(..)...`
            for c in calls:
                c.acc_start, c.acc_text, c.chain_n, c.chain_end, c.alone = a_start, acc, len(calls), chain_end, alone
                c.line_start = ls
                c.line_end_next = len(text) if le < 0 else le + 1
                self._take_call(bone, c)
            if alone or assign:
                bone.after = (len(text) if le < 0 else le + 1, before[:len(before) - len(before.lstrip())], le < 0)
        for b in self.bones.values():
            b.colliders = list(b.orig_colliders)
        for name in self.extra_names:
            if name not in self.bones:
                self.bones[name] = Bone(name, extra=True)

        for m in re.finditer(rf'(?<![\w.:]){pb}\s*:\s*setPreset\s*\(', code):
            close = match_paren(code, m.end() - 1)
            if close is None or not self._real(m.start(), self.pb_var):
                continue
            args = split_args(code[m.end():close])
            idm = re.match(r'^(["\'])(.*)\1$', args[0]) if args else None
            if not idm:
                continue
            vals = {}
            for key, a in zip(PRESET_ORDER, args[1:]):
                v = parse_value(a, PARAM_BY_KEY[key].kind)
                if v is not UNPARSED:
                    vals[key] = v
            self.script_presets[idm.group(2)] = vals

        for b in self.bones.values():
            self.resolve_bone(b)

        self.init_end = self.init_indent = None
        for rx in INIT_RES:
            m = rx.search(scode)
            if m:
                end = find_matching_end(scode, m.end())
                if end is not None:
                    self.init_end = end
                    body = text[m.end():end]
                    first = next((ln for ln in body.split("\n")[1:] if ln.strip()), None)
                    if first is not None:
                        self.init_indent = first[:len(first) - len(first.lstrip())]
                    else:
                        hdr_ls = text.rfind("\n", 0, m.start()) + 1
                        hdr = text[hdr_ls:m.start()]
                        base = hdr[:len(hdr) - len(hdr.lstrip())]
                        self.init_indent = base + ("\t" if "\t" in base else "    ")
                    break

    def _take_call(self, bone, c):
        raw = self.code[c.open + 1:c.close].strip()
        if c.method in SETTERS:
            p = SETTERS[c.method]
            v = parse_value(raw, p.kind)
            bone.calls[p.key] = c
            c.ctor = (VEC_RE.match(raw).group(1) if VEC_RE.match(raw) else "vec")
            if v is UNPARSED:
                bone.unparsed[p.key] = raw
                bone.orig.pop(p.key, None)
            else:
                bone.unparsed.pop(p.key, None)
                bone.orig[p.key] = v
        elif c.method in ("blacklistCollider", "unBlacklistCollider"):
            sm = re.match(r'^(["\'])(.*)\1$', raw)
            if sm:
                cid = sm.group(2)
                if c.method == "blacklistCollider":
                    bone.collider_calls.setdefault(cid, []).append(c)
                    if cid not in bone.orig_colliders:
                        bone.orig_colliders.append(cid)
                elif cid in bone.orig_colliders:
                    bone.orig_colliders.remove(cid)

    # ----- helpers ------------------------------------------------------------
    def preset_table(self):
        """Every preset a bone can start from: the API's own plus the script's setPreset() calls."""
        table = OrderedDict(API_PRESETS)
        for pid, vals in self.script_presets.items():
            d = copy.deepcopy(API_PRESETS["physBone"])
            d.update(copy.deepcopy(vals))
            table[pid] = d
        return table

    def resolve_bone(self, bone):
        """Give the bone the values of its preset (created with newPhysBone("x"), or picked by its group name)."""
        table = self.preset_table()
        pid = bone.preset_id
        if pid is None:
            cands = [k for k in table if bone.name.startswith(k)]
            pid = max(cands, key=len) if cands else None
        bone.preset_id = pid
        bone.base_vals = copy.deepcopy(table[pid]) if pid in table else {}

    def ensure_bone(self, name, extra=True):
        if name not in self.bones:
            self.bones[name] = Bone(name, extra=extra)
            self.resolve_bone(self.bones[name])
            if extra and name not in self.extra_names:
                self.extra_names.append(name)
        return self.bones[name]

    def dirty(self):
        return any(b.dirty() for b in self.bones.values())

    # ----- writing --------------------------------------------------------------
    def build_new_text(self):
        """Apply the UI changes to the script text. Returns (new_text, [human readable notes])."""
        text, code = self.text, self.code
        ops, notes = [], []
        inserts = {}            # pos -> text
        init_lines, tail = [], False
        for bone in self.bones.values():
            new_calls = []
            for p in PARAMS:
                if bone.state(p.key) != "edited":
                    continue
                cur = bone.get(p.key)
                call = bone.calls.get(p.key)
                if call is not None:
                    ctor = getattr(call, "ctor", "vec")
                    ops.append((call.open + 1, call.close, fmt_value(p, cur, ctor)))
                    notes.append(f"{bone.name}: {p.setter} -> {fmt_value(p, cur, ctor)}")
                else:
                    new_calls.append(f"{p.setter}({fmt_value(p, cur)})")
                    notes.append(f"{bone.name}: + {p.setter}({fmt_value(p, cur)})")
            for cid in bone.colliders:
                if cid not in bone.orig_colliders:
                    new_calls.append(f"blacklistCollider({lua_str(cid)})")
                    notes.append(f"{bone.name}: + blacklistCollider({lua_str(cid)})")
            for cid in bone.orig_colliders:
                if cid in bone.colliders:
                    continue
                for c in bone.collider_calls.get(cid, []):
                    if c.method != "blacklistCollider":
                        continue
                    if c.chain_n == 1 and c.alone:
                        ops.append((c.line_start, c.line_end_next, ""))
                    elif c.chain_n > 1:
                        ops.append((c.colon, c.close + 1, ""))
                    else:
                        ops.append((c.name_start, c.open, "unBlacklistCollider"))
                    notes.append(f"{bone.name}: - blacklistCollider({lua_str(cid)})")
            if not new_calls:
                continue
            acc = bone.accessor or lua_accessor(self.pb_var, bone.name)
            if bone.after is not None:
                pos, indent, need_nl = bone.after
                block = "".join(f"{indent}{acc}:{c}\n" for c in new_calls)
                inserts[pos] = inserts.get(pos, "") + (("\n" + block[:-1]) if need_nl else block)
            else:
                init_lines += [f"{acc}:{c}" for c in new_calls]
        if init_lines:
            if self.init_end is not None:
                end = self.init_end
                ls = text.rfind("\n", 0, end) + 1
                block = "".join(f"{self.init_indent}{ln}\n" for ln in init_lines)
                if text[ls:end].strip() == "":
                    inserts[ls] = inserts.get(ls, "") + block
                else:
                    inserts[end] = inserts.get(end, "") + "\n" + block
            else:
                tail = True
        for pos, t in inserts.items():
            ops.append((pos, pos, t))
        out = text
        for s, e, t in sorted(ops, key=lambda o: (o[0], o[1]), reverse=True):
            out = out[:s] + t + out[e:]
        if tail:
            sep = "" if out.endswith("\n") or not out else "\n"
            out += sep + "\nfunction events.entity_init()\n" + "".join(f"    {ln}\n" for ln in init_lines) + "end\n"
            notes.append("added a new events.entity_init() function")
        if (ops or tail) and not self.has_require:
            out = f"local {self.pb_var} = require('physBoneAPI')\n" + out
            notes.append("added: local physBone = require('physBoneAPI')")
        return out, notes


def make_setpreset(name, values, pb_var="physBone"):
    args = []
    for key in PRESET_ORDER:
        p = PARAM_BY_KEY[key]
        v = values.get(key)
        args.append("nil" if v is None or same(v, default_value(p)) else fmt_value(p, v))
    while args and args[-1] == "nil":
        args.pop()
    return f"{pb_var}:setPreset({lua_str(name)}" + "".join(", " + a for a in args) + ")"


def bbmodel_bones(path, extra_prefixes=()):
    with open(path, "r", encoding="utf-8-sig") as fh:
        data = json.load(fh)
    prefixes = tuple(BONE_PREFIXES) + tuple(extra_prefixes)
    found = []
    group_defs = {g["uuid"]: g for g in (data.get("groups") or []) if isinstance(g, dict) and g.get("uuid")}

    def walk(node):
        if isinstance(node, dict):
            n = node.get("name") or (group_defs.get(node.get("uuid")) or {}).get("name")
            if isinstance(n, str) and n.startswith(prefixes) and n not in found:
                found.append(n)
            walk(node.get("children", []))
        elif isinstance(node, list):
            for x in node:
                walk(x)
    walk(data.get("outliner", []))
    return found


# ---- preset library -----------------------------------------------------------
def clean_preset(raw):
    out = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        p = PARAM_BY_KEY.get(k)
        if not p:
            continue
        if p.kind == "vec3" and isinstance(v, list) and len(v) == 3 and all(isinstance(x, (int, float)) for x in v):
            out[k] = [float(x) for x in v]
        elif p.kind == "num" and isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = float(v)
    return out


def load_library(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return OrderedDict()
    lib = OrderedDict()
    for name, vals in (data.get("presets", {}) if isinstance(data, dict) else {}).items():
        c = clean_preset(vals)
        if c:
            lib[str(name)] = c
    return lib


def save_library(path, lib):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"presets": lib}, fh, indent=2)


def load_settings():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as fh:
            d = json.load(fh)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(d):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as fh:
            json.dump(d, fh, indent=2)
    except OSError:
        pass


def guess_avatar_dir():
    cands = []
    if os.environ.get("APPDATA"):
        cands.append(os.path.join(os.environ["APPDATA"], ".minecraft", "figura", "avatars"))
    home = os.path.expanduser("~")
    cands += [os.path.join(home, ".minecraft", "figura", "avatars"),
              os.path.join(home, "Library", "Application Support", "minecraft", "figura", "avatars")]
    return next((c for c in cands if os.path.isdir(c)), home)


# ==========================================================================
# Live preview, part 1 (no GUI): textures, .bbmodel loader, tiny 3D maths, approximate physics
# ==========================================================================
IDENT = (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0)   # 3x4 affine matrix, row-major


def v_add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def v_sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def v_mul(a, s):
    return (a[0] * s, a[1] * s, a[2] * s)


def v_dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def v_cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def v_len(a):
    return math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])


def v_norm(a, fallback=(0.0, -1.0, 0.0)):
    n = v_len(a)
    return fallback if n < 1e-9 else (a[0] / n, a[1] / n, a[2] / n)


def m_mul(a, b):
    return (a[0] * b[0] + a[1] * b[4] + a[2] * b[8], a[0] * b[1] + a[1] * b[5] + a[2] * b[9],
            a[0] * b[2] + a[1] * b[6] + a[2] * b[10], a[0] * b[3] + a[1] * b[7] + a[2] * b[11] + a[3],
            a[4] * b[0] + a[5] * b[4] + a[6] * b[8], a[4] * b[1] + a[5] * b[5] + a[6] * b[9],
            a[4] * b[2] + a[5] * b[6] + a[6] * b[10], a[4] * b[3] + a[5] * b[7] + a[6] * b[11] + a[7],
            a[8] * b[0] + a[9] * b[4] + a[10] * b[8], a[8] * b[1] + a[9] * b[5] + a[10] * b[9],
            a[8] * b[2] + a[9] * b[6] + a[10] * b[10], a[8] * b[3] + a[9] * b[7] + a[10] * b[11] + a[11])


def m_apply(m, v):
    x, y, z = v
    return (m[0] * x + m[1] * y + m[2] * z + m[3], m[4] * x + m[5] * y + m[6] * z + m[7],
            m[8] * x + m[9] * y + m[10] * z + m[11])


def m_dir(m, v):
    x, y, z = v
    return (m[0] * x + m[1] * y + m[2] * z, m[4] * x + m[5] * y + m[6] * z, m[8] * x + m[9] * y + m[10] * z)


def m_dir_t(m, v):
    """Apply the transpose (= inverse, for rotations) of the linear part."""
    x, y, z = v
    return (m[0] * x + m[4] * y + m[8] * z, m[1] * x + m[5] * y + m[9] * z, m[2] * x + m[6] * y + m[10] * z)


def m_translate(t):
    return (1.0, 0.0, 0.0, t[0], 0.0, 1.0, 0.0, t[1], 0.0, 0.0, 1.0, t[2])


def m_rot_zyx(rx, ry, rz):
    """Blockbench Euler rotation (degrees), order ZYX: R = Rz * Ry * Rx."""
    cx, sx = math.cos(math.radians(rx)), math.sin(math.radians(rx))
    cy, sy = math.cos(math.radians(ry)), math.sin(math.radians(ry))
    cz, sz = math.cos(math.radians(rz)), math.sin(math.radians(rz))
    rxm = (1, 0, 0, 0, 0, cx, -sx, 0, 0, sx, cx, 0)
    rym = (cy, 0, sy, 0, 0, 1, 0, 0, -sy, 0, cy, 0)
    rzm = (cz, -sz, 0, 0, sz, cz, 0, 0, 0, 0, 1, 0)
    return m_mul(rzm, m_mul(rym, rxm))


def m_about(r, p):
    """Rotation matrix r (no translation) applied about the point p."""
    rp = m_dir(r, p)
    return (r[0], r[1], r[2], p[0] - rp[0], r[4], r[5], r[6], p[1] - rp[1], r[8], r[9], r[10], p[2] - rp[2])


def m_rx(deg):
    c, sn = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return (1.0, 0.0, 0.0, 0.0, 0.0, c, -sn, 0.0, 0.0, sn, c, 0.0)


def m_ry(deg):
    c, sn = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return (c, 0.0, sn, 0.0, 0.0, 1.0, 0.0, 0.0, -sn, 0.0, c, 0.0)


def m_rz(deg):
    c, sn = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return (c, -sn, 0.0, 0.0, sn, c, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0)


R_Y90, R_YM90 = m_ry(90.0), m_ry(-90.0)       # newPhysBone() does part:setRot(0,90,0); the render code undoes it


# ---- textures (own tiny PNG reader, so no extra libraries are needed) ----------------
def decode_png(buf):
    """Minimal PNG reader (8-bit, non-interlaced) -> (width, height, RGBA bytes)."""
    if buf[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat, plte, trns, hdr = 8, [], None, None, None
    while pos + 8 <= len(buf):
        ln = struct.unpack(">I", buf[pos:pos + 4])[0]
        typ, data = buf[pos + 4:pos + 8], buf[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if typ == b"IHDR":
            hdr = struct.unpack(">IIBBBBB", data)
        elif typ == b"PLTE":
            plte = data
        elif typ == b"tRNS":
            trns = data
        elif typ == b"IDAT":
            idat.append(data)
        elif typ == b"IEND":
            break
    if hdr is None or not idat:
        raise ValueError("bad PNG")
    w, h, depth, ctype, _c, _f, interlace = hdr
    if depth != 8 or interlace != 0 or ctype not in (0, 2, 3, 4, 6):
        raise ValueError("unsupported PNG (need 8-bit, non-interlaced)")
    ch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    stride = w * ch
    raw = zlib.decompress(b"".join(idat))
    if len(raw) < h * (stride + 1):
        raise ValueError("truncated PNG")
    rows, prev, p = [], bytearray(stride), 0
    for _ in range(h):
        ft = raw[p]
        line = bytearray(raw[p + 1:p + 1 + stride])
        p += 1 + stride
        if ft == 1:
            for i in range(ch, stride):
                line[i] = (line[i] + line[i - ch]) & 255
        elif ft == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif ft == 3:
            for i in range(stride):
                left = line[i - ch] if i >= ch else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 255
        elif ft == 4:
            for i in range(stride):
                a = line[i - ch] if i >= ch else 0
                b = prev[i]
                c = prev[i - ch] if i >= ch else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                line[i] = (line[i] + (a if (pa <= pb and pa <= pc) else (b if pb <= pc else c))) & 255
        elif ft != 0:
            raise ValueError("bad PNG filter")
        rows.append(line)
        prev = line
    if ctype == 6:
        return w, h, b"".join(bytes(r) for r in rows)
    out = bytearray()
    for line in rows:
        for x in range(w):
            if ctype == 2:
                out += bytes((line[3 * x], line[3 * x + 1], line[3 * x + 2], 255))
            elif ctype == 0:
                out += bytes((line[x], line[x], line[x], 255))
            elif ctype == 4:
                out += bytes((line[2 * x], line[2 * x], line[2 * x], line[2 * x + 1]))
            else:
                i = line[x]
                out += bytes((plte[3 * i], plte[3 * i + 1], plte[3 * i + 2], trns[i] if trns and i < len(trns) else 255))
    return w, h, bytes(out)


class TextureSet:
    """The model's textures, decoded once; hands out per-face grids of texel colours."""

    def __init__(self, data, base_dir=""):
        res = data.get("resolution") if isinstance(data.get("resolution"), dict) else {}
        self.tex = []
        for t in data.get("textures", []) or []:
            entry = None
            try:
                w, h, px = decode_png(self._bytes(t, base_dir))
                entry = (w, h, px, float(t.get("uv_width") or res.get("width") or w),
                         float(t.get("uv_height") or res.get("height") or h))
            except Exception:                       # unreadable texture -> faces fall back to flat colour
                entry = None
            self.tex.append(entry)

    @staticmethod
    def _bytes(t, base_dir):
        if not isinstance(t, dict):
            raise ValueError("bad texture entry")
        src = t.get("source")
        if isinstance(src, str) and src.startswith("data:") and "," in src:
            return base64.b64decode(src.split(",", 1)[1])
        for key in ("relative_path", "path"):
            rel = t.get(key)
            if isinstance(rel, str) and base_dir:
                cand = os.path.normpath(os.path.join(base_dir, rel))
                if os.path.isfile(cand):
                    with open(cand, "rb") as fh:
                        return fh.read()
        raise ValueError("no texture data")

    def usable(self):
        return sum(1 for t in self.tex if t is not None)

    def grid(self, ti, uv, rot=0, cap=16):
        """(rows of (r,g,b)|None, nu, nv, average|None) for the uv rectangle, or None if unavailable.
        Row 0 is the face's top edge, column 0 its left edge (the (u1, v1) corner)."""
        if not isinstance(ti, int) or isinstance(ti, bool) or not (0 <= ti < len(self.tex)) or self.tex[ti] is None:
            return None
        w, h, px, uvw, uvh = self.tex[ti]
        u1, v1, u2, v2 = (float(x) for x in uv)
        nu = max(1, min(cap, int(round(abs(u2 - u1) * w / uvw))))
        nv = max(1, min(cap, int(round(abs(v2 - v1) * h / uvh))))
        grid, tot, cnt = [], [0, 0, 0], 0
        for j in range(nv):
            y = min(h - 1, max(0, int((v1 + (v2 - v1) * (j + 0.5) / nv) * h / uvh)))
            row = []
            for i in range(nu):
                x = min(w - 1, max(0, int((u1 + (u2 - u1) * (i + 0.5) / nu) * w / uvw)))
                o = (y * w + x) * 4
                if px[o + 3] < 128:
                    row.append(None)
                else:
                    row.append((px[o], px[o + 1], px[o + 2]))
                    tot[0] += px[o]
                    tot[1] += px[o + 1]
                    tot[2] += px[o + 2]
                    cnt += 1
            grid.append(row)
        for _ in range((int(rot or 0) // 90) % 4):          # face rotation, clockwise
            grid = [list(r) for r in zip(*grid[::-1])]
        avg = None if cnt == 0 else (tot[0] // cnt, tot[1] // cnt, tot[2] // cnt)
        return grid, len(grid[0]), len(grid), avg


# ---- scene --------------------------------------------------------------------
PALETTE = [(162, 235, 255), (255, 245, 153), (255, 165, 165), (189, 177, 255),
           (255, 203, 153), (204, 255, 153), (255, 153, 255), (153, 204, 255)]
DEFAULT_RGB = (154, 165, 177)
# vertex indices around each face (used for culling/winding); corner i = x + 2*y + 4*z (0 = from, 1 = to)
CUBE_FACES = [("north", (0, 1, 3, 2)), ("south", (4, 5, 7, 6)), ("down", (0, 1, 5, 4)),
              ("up", (2, 3, 7, 6)), ("west", (0, 2, 6, 4)), ("east", (1, 3, 7, 5))]
# texture corners (top-left, bottom-left, bottom-right, top-right) of each face, as Minecraft/Blockbench map them
CUBE_UV = {"north": (3, 1, 0, 2), "south": (6, 4, 5, 7), "down": (4, 0, 1, 5),
           "up": (2, 6, 7, 3), "west": (2, 0, 4, 6), "east": (7, 5, 1, 3)}


class Elem:
    """One drawable: model-space vertices (the element's own rotation already applied) and faces.
    faces: list of (vertex-index tuple, (r, g, b) | None, two_sided, tex | None)
    tex = (grid, nu, nv, (tl, bl, br, tr) vertex indices, uniform colour | False)"""
    __slots__ = ("verts", "faces")

    def __init__(self, verts, faces):
        self.verts, self.faces = verts, faces


LIMB_NAMES = {"leftarm": "LA", "rightarm": "RA", "leftleg": "LL", "rightleg": "RL"}


class Node:
    def __init__(self, name, origin, rot, parent):
        self.name, self.origin, self.parent = name, tuple(float(x) for x in origin), parent
        self.local = m_about(m_rot_zyx(*[float(x) for x in rot]), self.origin)
        self.children, self.elems = [], []
        self.phys = False
        self.under_phys = False
        self.world = IDENT
        self.limb = LIMB_NAMES.get(name.lower().replace("_", "").replace(" ", ""))


def _face_style(textures, fd, fallback, uvidx):
    """-> (flat colour | None, tex | None) for one face."""
    tx = fd.get("texture")
    if tx is False:
        return None, None
    uv = fd.get("uv")
    if textures is not None and isinstance(tx, int) and isinstance(uv, list) and len(uv) == 4:
        g = textures.grid(tx, uv, fd.get("rotation", 0))
        if g is not None:
            grid, nu, nv, avg = g
            if avg is None:
                return None, None                       # completely transparent
            first = grid[0][0]
            uniform = first if all(c == first for row in grid for c in row) else False
            return avg, (grid, nu, nv, uvidx, uniform)
    return fallback, None


def _cube(el, textures):
    f, t = el.get("from"), el.get("to")
    if not (isinstance(f, list) and isinstance(t, list) and len(f) == 3 and len(t) == 3):
        return None
    o = el.get("origin", [0, 0, 0])
    m = m_about(m_rot_zyx(*[float(x) for x in el.get("rotation", [0, 0, 0])]), tuple(float(x) for x in o))
    inf = float(el.get("inflate", 0) or 0)
    verts = []
    for i in range(8):
        verts.append(m_apply(m, (t[0] + inf if i & 1 else f[0] - inf,
                                 t[1] + inf if i & 2 else f[1] - inf,
                                 t[2] + inf if i & 4 else f[2] - inf)))
    cen = (sum(v[0] for v in verts) / 8, sum(v[1] for v in verts) / 8, sum(v[2] for v in verts) / 8)
    col = el.get("color")
    fallback = PALETTE[col % 8] if isinstance(col, int) else DEFAULT_RGB
    fdata = el.get("faces")
    faces = []
    for name, idx in CUBE_FACES:
        if isinstance(fdata, dict) and fdata:
            fd = fdata.get(name)
            if not isinstance(fd, dict):
                continue
            rgb, tex = _face_style(textures, fd, fallback, CUBE_UV[name])
        else:
            rgb, tex = fallback, None
        a, b, c = verts[idx[0]], verts[idx[1]], verts[idx[2]]
        fc = tuple(sum(verts[i][k] for i in idx) / 4 for k in range(3))
        if v_dot(v_cross(v_sub(b, a), v_sub(c, a)), v_sub(fc, cen)) < 0:
            idx = idx[::-1]                      # make the winding point outwards
        faces.append((idx, rgb, False, tex))
    return Elem(verts, faces)


def _mesh(el, textures):
    vmap = el.get("vertices")
    if not isinstance(vmap, dict) or not vmap:
        return None
    o = tuple(float(x) for x in el.get("origin", [0, 0, 0]))
    m = m_about(m_rot_zyx(*[float(x) for x in el.get("rotation", [0, 0, 0])]), o)
    keys = list(vmap)
    index = {k: i for i, k in enumerate(keys)}
    verts = [m_apply(m, v_add(o, tuple(float(x) for x in vmap[k]))) for k in keys]
    col = el.get("color")
    fallback = PALETTE[col % 8] if isinstance(col, int) else DEFAULT_RGB
    faces = []
    for fd in (el.get("faces") or {}).values():
        if not isinstance(fd, dict):
            continue
        ids = [k for k in fd.get("vertices", []) if k in index]
        if len(ids) < 3:
            continue
        uvd = fd.get("uv") if isinstance(fd.get("uv"), dict) else {}
        uvs = [uvd[k] for k in ids if k in uvd]
        rgb = fallback
        if fd.get("texture") is False:
            rgb = None
        elif uvs and textures is not None and isinstance(fd.get("texture"), int):
            us, vs = [u[0] for u in uvs], [u[1] for u in uvs]
            g = textures.grid(fd["texture"], [min(us), min(vs), max(us), max(vs)], 0, 4)
            if g is not None:
                rgb = g[3]                       # meshes: flat average colour of their uv area
        faces.append((tuple(index[k] for k in ids), rgb, True, None))
    return Elem(verts, faces)


class Scene:
    def __init__(self):
        self.nodes, self.root = [], None
        self.center, self.radius, self.floor_y = (0.0, 12.0, 0.0), 32.0, 0.0
        self.phys_names, self.name = [], ""
        self.n_elems = self.n_faces = 0
        self.textures = None
        self.tex_note = ""


def build_scene(data, prefixes=BONE_PREFIXES, textures=None, name="", names=()):
    elems = {}
    for el in data.get("elements", []) or []:
        if not isinstance(el, dict) or el.get("visibility") is False:
            continue
        kind = el.get("type", "cube")
        e = _cube(el, textures) if kind == "cube" else (_mesh(el, textures) if kind == "mesh" else None)
        if e is not None and el.get("uuid"):
            elems[el["uuid"]] = e
    group_defs = {g["uuid"]: g for g in (data.get("groups") or []) if isinstance(g, dict) and g.get("uuid")}
    root = Node("<root>", (0, 0, 0), (0, 0, 0), None)
    nodes = [root]
    prefixes, names = tuple(prefixes), set(names)

    def add(parent, item):
        if isinstance(item, str):
            e = elems.get(item)
            if e is not None:
                parent.elems.append(e)
        elif isinstance(item, dict):
            g = group_defs.get(item.get("uuid"))
            if g:                                   # newer Blockbench files keep a group's name/origin/rotation in "groups"
                item = {**g, **item}
            if item.get("visibility") is False:
                return
            nm = str(item.get("name", ""))
            n = Node(nm, item.get("origin", [0, 0, 0]), item.get("rotation", [0, 0, 0]), parent)
            n.phys = bool(nm) and (nm.startswith(prefixes) or nm in names)
            n.under_phys = n.phys or parent.under_phys
            nodes.append(n)
            parent.children.append(n)
            for ch in item.get("children", []) or []:
                add(n, ch)
    for it in data.get("outliner", []) or []:
        add(root, it)

    sc = Scene()
    sc.nodes, sc.root, sc.name, sc.textures = nodes, root, name, textures
    lo, hi = [1e9] * 3, [-1e9] * 3
    for n in nodes:
        if n.parent is not None:
            n.world = m_mul(n.parent.world, n.local)
        for el in n.elems:
            sc.n_elems += 1
            sc.n_faces += len(el.faces)
            for v in el.verts:
                w = m_apply(n.world, v)
                for k in range(3):
                    lo[k], hi[k] = min(lo[k], w[k]), max(hi[k], w[k])
    if sc.n_elems:
        sc.center = tuple((lo[k] + hi[k]) / 2 for k in range(3))
        sc.radius = max(8.0, v_len(v_sub(tuple(hi), tuple(lo))) / 2)
        sc.floor_y = lo[1]
    sc.phys_names = [n.name for n in nodes if n.phys]
    return sc


def load_scene(path, prefixes=BONE_PREFIXES, names=(), textured=True):
    with open(path, "r", encoding="utf-8-sig") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("not a .bbmodel file")
    textures = TextureSet(data, os.path.dirname(os.path.abspath(path))) if textured else None
    sc = build_scene(data, prefixes, textures, os.path.basename(path), names)
    n_tex = len(data.get("textures") or [])
    if textured and n_tex == 0:
        sc.tex_note = "This model has no textures, so parts are drawn in flat colours."
    elif textured and textures.usable() < n_tex:
        sc.tex_note = (f"{n_tex - textures.usable()} of {n_tex} texture(s) couldn't be read "
                       "(only embedded or linked 8-bit PNGs are supported); those faces are flat-coloured.")
    return sc


# ---- physics: a port of physBoneRender() from physBoneAPI.lua --------------------------
LOCKED_FPS = 60.0                  # the preview is locked to 60 fps: the API's physics depends on frame rate
FRAME_TIME = 1.0 / LOCKED_FPS


class PhysSim:
    """The API's own algorithm, per physBone group (collisions excluded). Everything below mirrors the Lua:

      * Each bone is a rigid rod: its tip `pos` (world, in blocks) always sits `length/16` blocks from the pivot.
      * Per game frame (dt ticks):  V = lastDisplacement / dt / (simSpeed*mass/100)
            V -= V * airResistance * dt / mass                    (drag against world velocity)
            V += equilibriumDir * springForce * dt / mass^2       (a CONSTANT push along `equilibrium`; zero vector = none)
            V += force * dt / mass^2                              (world-space force)
            V.y += gravity * dt / mass
            tip' = pivot + normalize(tip + V*dt*(simSpeed*mass/100) - pivot) * length/16
      * The model parts are then rotated about the pivot: rotMod, rollMod, then the swing computed from the tip
        direction (with vecMod scaling its axes). The bone's direction is therefore set by rotMod, not by the shape.
    `fps` is the game frame rate being imitated: the algorithm's results depend on it.
    """
    TICK = 20.0

    def __init__(self, scene, get_params):
        self.scene, self.get_params = scene, get_params
        self.fps = LOCKED_FPS
        self.state, self.debug = {}, {}
        self._acc = 0.0

    def reset(self):
        self.state.clear()
        self.debug.clear()
        self._acc = 0.0

    def update(self, dt, root_m, pose=None):
        dt = min(max(dt, 0.0), 0.1)
        self._acc += dt * self.fps
        steps = int(self._acc + 1e-9)
        self._acc = max(0.0, self._acc - steps)
        steps = min(steps, 12)
        sc = self.scene
        sc.root.world = root_m
        for n in sc.nodes[1:]:
            w = m_mul(n.parent.world, n.local)
            ang = pose.get(n.limb, 0.0) if (pose and n.limb) else 0.0
            if ang:                                      # limb swing, about the limb's own pivot
                w = m_mul(w, m_about(m_rx(ang), n.origin))
            n.world = self._phys(n, w, steps) if n.phys else w

    def _phys(self, n, w_rest, steps):
        prm = self.get_params(n.name)
        o = n.origin
        base = v_mul(m_apply(w_rest, o), 1.0 / 16.0)                 # pivot, in blocks
        part_m = m_mul(w_rest, R_Y90)                                 # the group as the API sees it
        length = max(float(prm["length"]), 0.0) / 16.0
        mass = max(float(prm["mass"]), 0.05)
        sim_speed = float(prm["simSpeed"])
        grav, air, spring = float(prm["gravity"]), float(prm["airResistance"]), float(prm["springForce"])
        eq, force = prm["equilibrium"], tuple(prm["force"])
        dir_s = v_norm(m_dir(w_rest, eq), (0.0, 0.0, 0.0)) if v_len(eq) > 1e-9 else (0.0, 0.0, 0.0)
        st = self.state.get(n)
        if st is None:
            st = self.state[n] = {"pos": base, "vel": (0.0, 0.0, 0.0), "base": base}      # starts AT the pivot, like the API
        dt = self.TICK / self.fps
        if sim_speed <= 0:                       # the Lua divides by zero here and the bone breaks; keep it hanging
            st["pos"], st["vel"], st["base"] = v_add(base, (0.0, -length, 0.0)), (0.0, 0.0, 0.0), base
        elif steps > 0:
            conv = sim_speed * mass / 100.0
            b0, pos, vel = st["base"], st["pos"], st["vel"]
            has_force = v_len(force) > 0.0
            for k in range(1, steps + 1):
                f = k / steps
                bk = (b0[0] + (base[0] - b0[0]) * f, b0[1] + (base[1] - b0[1]) * f, b0[2] + (base[2] - b0[2]) * f)
                v = v_mul(vel, 1.0 / dt / conv)
                if air != 0.0:
                    v = v_add(v, v_mul(v, -air * dt / mass))
                if spring != 0.0:
                    v = v_add(v, v_mul(dir_s, spring * dt / (mass * mass)))
                if has_force:
                    v = v_add(v, v_mul(force, dt / (mass * mass)))
                v = (v[0], v[1] + grav * dt / mass, v[2])
                pred = v_add(pos, v_mul(v, dt * conv))
                nxt = v_add(bk, v_mul(v_norm(v_sub(pred, bk), (0.0, -1.0, 0.0)), length))
                vel, pos = v_sub(nxt, pos), nxt
            if not all(math.isfinite(x) for x in pos + vel):
                pos, vel = v_add(base, (0.0, -length, 0.0)), (0.0, 0.0, 0.0)
            st["pos"], st["vel"], st["base"] = pos, vel, base
        # --- rotate the model parts, exactly as the API does ---
        d_loc = v_norm(m_dir_t(part_m, v_sub(st["pos"], base)), (0.0, -1.0, 0.0))     # tip direction in the part's frame
        vm = prm["vecMod"]
        rel = v_norm((d_loc[0] * vm[2], d_loc[1] * vm[1], d_loc[2] * vm[0]), (0.0, -1.0, 0.0))
        v2 = (rel[0], rel[2], -rel[1])                                                 # rotateAroundAxis(90, rel, (-1,0,0))
        pitch = math.degrees(math.asin(max(-1.0, min(1.0, -v2[1]))))
        yaw = math.degrees(math.atan2(v2[0], v2[2]))
        rm = prm["rotMod"]
        mods = m_mul(m_rx(rm[0]), m_mul(m_ry(rm[1]), m_rz(rm[2])))                    # rotate z, then y, then x
        swing = m_mul(m_rz(yaw), m_rx(pitch))
        comb = m_mul(R_Y90, m_mul(swing, m_mul(R_YM90, m_mul(m_ry(float(prm["rollMod"])), mods))))
        # where the bone settles when nothing moves: along gravity + the spring/force push
        net = (0.0, grav, 0.0)
        push = v_add(v_mul(dir_s, spring), force)
        net = v_add(net, v_mul(push, 1.0 / mass))
        rest = v_norm(net, (0.0, -1.0, 0.0))
        self.debug[n] = (v_mul(base, 16.0), v_mul(st["pos"], 16.0), v_mul(v_add(base, v_mul(rest, length)), 16.0), rest)
        return m_mul(w_rest, m_about(comb, o))


# ---- camera + software renderer ------------------------------------------------------
class Camera:
    near = 1.0

    def __init__(self, target, dist):
        self.target, self.dist = tuple(target), float(dist)
        self.yaw, self.pitch = 1.15, 0.22        # three-quarter side view, so forward walking is easy to read
        self.pos = self.fwd = self.right = self.up = self.light = (0.0, 0.0, 1.0)
        self.f, self.cx, self.cy = 600.0, 0.0, 0.0

    def setup(self, w, h):
        cp = math.cos(self.pitch)
        self.pos = (self.target[0] + self.dist * math.sin(self.yaw) * cp,
                    self.target[1] + self.dist * math.sin(self.pitch),
                    self.target[2] - self.dist * math.cos(self.yaw) * cp)
        self.fwd = v_norm(v_sub(self.target, self.pos))
        self.right = v_norm(v_cross(self.fwd, (0.0, 1.0, 0.0)), (1.0, 0.0, 0.0))
        self.up = v_cross(self.right, self.fwd)
        self.light = v_norm(v_add(v_add(v_mul(self.fwd, -0.8), v_mul(self.up, 0.5)), v_mul(self.right, -0.25)))
        self.f = 0.5 * h / math.tan(math.radians(22.5))
        self.cx, self.cy = w / 2.0, h / 2.0

    def project(self, p):
        d = v_sub(p, self.pos)
        z = v_dot(d, self.fwd)
        if z < self.near:
            return None
        return (self.cx + self.f * v_dot(d, self.right) / z, self.cy - self.f * v_dot(d, self.up) / z, z)


def _shade(rgb, lvl, cache):
    key = (rgb, lvl)
    col = cache.get(key)
    if col is None:
        s = 0.38 + 0.62 * lvl / 15.0
        col = cache[key] = "#%02x%02x%02x" % (int(rgb[0] * s), int(rgb[1] * s), int(rgb[2] * s))
    return col


def _emit_textured(polys, depth, proj, tex, lvl, cache):
    """Draw a textured quad as flat-coloured strips (one per run of equal texels), texel-accurate."""
    grid, nu, nv, uvidx, uniform = tex
    tl, bl, br, tr = (proj[i] for i in uvidx)
    if uniform is not False:
        if uniform is not None:
            polys.append((depth, [tl[0], tl[1], tr[0], tr[1], br[0], br[1], bl[0], bl[1]], _shade(uniform, lvl, cache)))
        return
    wpx = max(math.hypot(tr[0] - tl[0], tr[1] - tl[1]), math.hypot(br[0] - bl[0], br[1] - bl[1]))
    hpx = max(math.hypot(bl[0] - tl[0], bl[1] - tl[1]), math.hypot(br[0] - tr[0], br[1] - tr[1]))
    su, sv = min(nu, max(1, int(wpx / 3.0))), min(nv, max(1, int(hpx / 3.0)))

    def corner(u, v):
        ax, ay = tl[0] + (tr[0] - tl[0]) * u, tl[1] + (tr[1] - tl[1]) * u
        bx, by = bl[0] + (br[0] - bl[0]) * u, bl[1] + (br[1] - bl[1]) * u
        return ax + (bx - ax) * v, ay + (by - ay) * v

    for j in range(sv):
        row = grid[min(nv - 1, int((j + 0.5) * nv / sv))]
        v0, v1 = j / sv, (j + 1) / sv
        i = 0
        while i < su:
            c = row[min(nu - 1, int((i + 0.5) * nu / su))]
            k = i + 1
            while k < su and row[min(nu - 1, int((k + 0.5) * nu / su))] == c:
                k += 1
            if c is not None:
                u0, u1 = i / su, k / su
                x0, y0 = corner(u0, v0)
                x1, y1 = corner(u1, v0)
                x2, y2 = corner(u1, v1)
                x3, y3 = corner(u0, v1)
                polys.append((depth, [x0, y0, x1, y1, x2, y2, x3, y3], _shade(c, lvl, cache)))
            i = k


def render_scene(scene, cam, w, h, show_static=True, textured=True):
    """Return [(depth, [x0, y0, x1, y1, ...], '#rrggbb')] sorted far -> near."""
    cam.setup(w, h)
    polys, cache = [], {}
    for node in scene.nodes:
        if not node.elems or (not show_static and not node.under_phys):
            continue
        wm = node.world
        for el in node.elems:
            pw = [m_apply(wm, v) for v in el.verts]
            proj = [cam.project(p) for p in pw]
            for idx, rgb, two_sided, tex in el.faces:
                if rgb is None:
                    continue
                pts = [proj[i] for i in idx]
                if None in pts:
                    continue
                a, b, c = pw[idx[0]], pw[idx[1]], pw[idx[2]]
                n = v_cross(v_sub(b, a), v_sub(c, a))
                ln = v_len(n)
                if ln < 1e-12:
                    continue
                n = (n[0] / ln, n[1] / ln, n[2] / ln)
                if v_dot(n, v_sub(cam.pos, a)) <= 0:
                    if not two_sided:
                        continue
                    n = (-n[0], -n[1], -n[2])
                lvl = int(max(0.0, v_dot(n, cam.light)) * 15)
                depth = sum(q[2] for q in pts) / len(pts)
                if tex is not None and textured:
                    _emit_textured(polys, depth, proj, tex, lvl, cache)
                else:
                    flat = []
                    for q in pts:
                        flat += (q[0], q[1])
                    polys.append((depth, flat, _shade(rgb, lvl, cache)))
    polys.sort(key=lambda t: -t[0])
    return polys


MOTIONS = ["Walk", "Sprint", "Start & stop", "Still", "Jump", "Spin", "Shake"]
FOLLOW_MODES = ("Walk", "Sprint", "Start & stop")      # the camera travels with the model in these


def motion_state(mode, t):
    """(offset in Blockbench units, spin in degrees) of the whole model at time t.
    Walking speeds are the real ones: walk 4.3 blocks/s, sprint 5.6 blocks/s (1 block = 16 units)."""
    if mode == "Walk":                                 # forward = -Z, the way a Blockbench model faces
        return (0.0, 0.0, -69.0 * t), 0.0              # Minecraft's walk has no body bob: only the limbs move
    if mode == "Sprint":
        return (0.0, 0.0, -90.0 * t), 0.0
    if mode == "Start & stop":                         # accelerates to walking speed and back to a halt, every 4 s
        return (0.0, 0.0, -69.0 * (0.5 * t - math.sin(math.pi * t / 2.0) / math.pi)), 0.0
    if mode == "Jump":                                 # hops forward and back, so bones swing as well as bounce
        return (0.0, 30.0 * abs(math.sin(2.0 * t)), -14.0 * math.sin(1.0 * t)), 0.0
    if mode == "Spin":                                 # turns while circling (a bone on the spin axis would never swing)
        return (8.0 * math.cos(2.4 * t), 0.0, 8.0 * math.sin(2.4 * t)), 150.0 * t
    if mode == "Shake":
        return (3.0 * math.sin(26.0 * t), 0.0, 3.0 * math.cos(21.0 * t)), 0.0
    return (0.0, 0.0, 0.0), 0.0


def limb_pose(mode, t):
    """Minecraft's own limb animation (LivingEntity walk animation + HumanoidModel), as rotations about X in degrees.
    limbSwing grows by ~4 per block walked (capped at 1 per tick, i.e. 20/s) and limbSwingAmount = speed/5 (max 1).
    Right arm / left leg are in phase, left arm / right leg are the opposite phase; positive = swings forward."""
    if mode == "Walk":
        swing, amount = 4.3 * 4.0 * t, 4.3 / 5.0
    elif mode == "Sprint":
        swing, amount = 20.0 * t, 1.0
    elif mode == "Start & stop":
        dist = 69.0 * (0.5 * t - math.sin(math.pi * t / 2.0) / math.pi) / 16.0        # blocks walked
        speed = 4.3 * 0.5 * (1.0 - math.cos(math.pi * t / 2.0))                       # blocks per second
        swing, amount = 4.0 * dist, min(1.0, speed / 5.0)
    else:
        return {}
    c = math.cos(swing * 0.6662)
    arm, leg = math.degrees(c * amount), math.degrees(c * 1.4 * amount)
    return {"LA": arm, "RA": -arm, "RL": leg, "LL": -leg}


def root_matrix(offset, spin_deg, center):
    return m_mul(m_translate(offset), m_about(m_rot_zyx(0.0, spin_deg, 0.0), center))


# ==========================================================================
# Live preview, part 2: the fast renderer (numpy + Pillow). The libraries are installed on demand.
# ==========================================================================
LIB_DIR = os.path.join(os.path.expanduser("~"), ".figura_physbone_editor", "libs")   # private install folder
_RENDER_LIBS = None          # None = not tried yet, False = unavailable, else (numpy, PIL.Image, PIL.ImageDraw)


def _add_lib_dir():
    if os.path.isdir(LIB_DIR) and LIB_DIR not in sys.path:
        sys.path.append(LIB_DIR)             # appended: a system install wins, ours is only the fallback
        importlib.invalidate_caches()


def render_libs():
    """(numpy, Image, ImageDraw) if they can be imported, else None. Never installs anything."""
    global _RENDER_LIBS
    if _RENDER_LIBS is None:
        _add_lib_dir()
        try:
            import numpy as np
            from PIL import Image, ImageDraw
            _RENDER_LIBS = (np, Image, ImageDraw)
        except Exception:
            _RENDER_LIBS = False
    return _RENDER_LIBS or None


def install_render_libs():
    """pip-install numpy + Pillow into a private folder (the system Python is never touched).
    Returns (ok, message)."""
    global _RENDER_LIBS
    try:
        os.makedirs(LIB_DIR, exist_ok=True)
    except OSError as e:
        return False, f"Can't create {LIB_DIR}: {e}"
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
           "--upgrade", "--target", LIB_DIR, "numpy", "pillow"]
    out, tried_ensurepip = "", False
    while True:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, str(e)
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        if r.returncode == 0:
            break
        if not tried_ensurepip and "No module named pip" in out:
            tried_ensurepip = True
            try:
                subprocess.run([sys.executable, "-m", "ensurepip", "--default-pip"],
                               capture_output=True, text=True, timeout=300)
            except (OSError, subprocess.TimeoutExpired):
                pass
            continue
        return False, out[-700:]
    _RENDER_LIBS = None
    importlib.invalidate_caches()
    if render_libs() is None:
        return False, "pip finished, but numpy/Pillow still can't be imported.\n" + out[-400:]
    return True, "Installed numpy and Pillow."


def ppm_bytes(img):
    """A PIL RGB image as binary PPM: Tk can read this itself, without Pillow's ImageTk."""
    return b"P6 %d %d 255\n" % img.size + img.tobytes()


class FastRenderer:
    """Same picture as render_scene(), much cheaper: numpy transforms/projects/culls/shades/sorts every
    face in bulk, Pillow draws the polygons in C, and each textured face is a single perspective warp of
    its texel grid (so the cost doesn't grow with the number of texels). The result is one image."""
    BG = (32, 35, 42)
    GRID = (47, 52, 64)

    def __init__(self, scene):
        libs = render_libs()
        if libs is None:
            raise RuntimeError("numpy and Pillow are required for the fast renderer")
        self.np, self.Image, self.ImageDraw = libs
        np, Image = self.np, self.Image
        self.T_PERSP = getattr(Image, "Transform", Image).PERSPECTIVE
        self.R_NEAREST = getattr(Image, "Resampling", Image).NEAREST
        self.scene = scene
        verts, vnode, fi, fcol, ftwo, fphys, ftex = [], [], [], [], [], [], []
        tex_arr, tex_uv = [], []
        for ni, node in enumerate(scene.nodes):
            for el in node.elems:
                base = len(verts)
                verts.extend(el.verts)
                vnode.extend([ni] * len(el.verts))
                for idx, rgb, two, tex in el.faces:
                    if rgb is None:
                        continue
                    g = [base + i for i in idx]
                    tris = [g] if len(g) <= 4 else [[g[0], g[i], g[i + 1]] for i in range(1, len(g) - 1)]
                    for t in tris:
                        fi.append((t + [t[-1]] * 4)[:4])           # triangles repeat their last vertex
                        fcol.append(rgb)
                        ftwo.append(bool(two))
                        fphys.append(bool(node.under_phys))
                        if tex is not None and len(g) == 4 and tex[4] is False:      # a real (non-uniform) texture
                            grid, nu, nv, uvidx, _uniform = tex
                            arr = np.zeros((nv, nu, 4), dtype=np.uint8)
                            for j, row in enumerate(grid):
                                for i2, c in enumerate(row):
                                    if c is not None:
                                        arr[j, i2] = (c[0], c[1], c[2], 255)
                            tex_arr.append(arr)
                            tex_uv.append([base + q for q in uvidx])
                            ftex.append(len(tex_arr) - 1)
                        else:
                            ftex.append(-1)
        self.V = np.array(verts, dtype=float).reshape(-1, 3)
        self.vnode = np.array(vnode, dtype=np.int64)
        self.fi = np.array(fi, dtype=np.int64).reshape(-1, 4)
        self.fcol = np.array(fcol, dtype=float).reshape(-1, 3)
        self.ftwo = np.array(ftwo, dtype=bool)
        self.fphys = np.array(fphys, dtype=bool)
        self.ftex = np.array(ftex, dtype=np.int64)
        self.tex_arr = tex_arr
        self.tex_uv = np.array(tex_uv, dtype=np.int64).reshape(-1, 4)
        self.tex_nuv = np.array([[a.shape[1], a.shape[0]] for a in tex_arr], dtype=float).reshape(-1, 2)
        self.tex_opaque = [bool((a[..., 3] == 255).all()) for a in tex_arr]   # no see-through texels -> safe to under-fill
        self._tex_cache = {}
        self.last_count = 0

    # ----- helpers -----------------------------------------------------------------
    def _shaded_tex(self, t, lvl):
        key = (t, lvl)
        im = self._tex_cache.get(key)
        if im is None:
            arr = self.tex_arr[t].copy()
            arr[..., :3] = (arr[..., :3] * (0.38 + 0.62 * lvl / 15.0)).astype(self.np.uint8)
            im = self._tex_cache[key] = self.Image.fromarray(arr)
        return im

    def _project(self, cam, P):
        np = self.np
        D = P - np.array(cam.pos)
        z = D @ np.array(cam.fwd)
        ok = z >= cam.near
        zs = np.where(ok, z, 1.0)
        return (cam.cx + cam.f * (D @ np.array(cam.right)) / zs, cam.cy - cam.f * (D @ np.array(cam.up)) / zs, z, ok)

    def _tex_quads(self, tex_ids, sx, sy, w, h):
        """For every textured face: the crop box on screen and Pillow's PERSPECTIVE coefficients
        (screen pixel -> texel). Solved for all faces in one batch."""
        np = self.np
        ks = np.nonzero(tex_ids >= 0)[0]
        if not len(ks):
            return {}
        t = tex_ids[ks]
        uvg = self.tex_uv[t]                                   # (K, 4): top-left, bottom-left, bottom-right, top-right
        X, Y = sx[uvg], sy[uvg]
        x0 = np.floor(np.clip(X.min(axis=1), 0, w)).astype(np.int64)
        x1 = np.ceil(np.clip(X.max(axis=1), 0, w)).astype(np.int64)
        y0 = np.floor(np.clip(Y.min(axis=1), 0, h)).astype(np.int64)
        y1 = np.ceil(np.clip(Y.max(axis=1), 0, h)).astype(np.int64)
        area = 0.5 * np.abs(sum(X[:, i] * Y[:, (i + 1) % 4] - X[:, (i + 1) % 4] * Y[:, i] for i in range(4)))
        ok = (x1 > x0) & (y1 > y0) & (area > 1.0)
        if not ok.any():
            return {}
        ks, t, X, Y, x0, x1, y0, y1 = (a[ok] for a in (ks, t, X, Y, x0, x1, y0, y1))
        Xs, Ys = X - x0[:, None], Y - y0[:, None]
        nu, nv = self.tex_nuv[t, 0], self.tex_nuv[t, 1]
        zero = np.zeros_like(nu)
        U = np.stack([zero, zero, nu, nu], axis=1)
        V = np.stack([zero, nv, nv, zero], axis=1)
        K = len(ks)
        A, rhs = np.zeros((K, 8, 8)), np.zeros((K, 8))
        for i in range(4):
            r = 2 * i
            A[:, r, 0], A[:, r, 1], A[:, r, 2] = Xs[:, i], Ys[:, i], 1.0
            A[:, r, 6], A[:, r, 7], rhs[:, r] = -U[:, i] * Xs[:, i], -U[:, i] * Ys[:, i], U[:, i]
            A[:, r + 1, 3], A[:, r + 1, 4], A[:, r + 1, 5] = Xs[:, i], Ys[:, i], 1.0
            A[:, r + 1, 6], A[:, r + 1, 7], rhs[:, r + 1] = -V[:, i] * Xs[:, i], -V[:, i] * Ys[:, i], V[:, i]
        try:
            coef = np.linalg.solve(A, rhs[..., None])[..., 0]
        except np.linalg.LinAlgError:                          # one bad (edge-on) face must not spoil the rest
            coef = np.full((K, 8), np.nan)
            for j in range(K):
                try:
                    coef[j] = np.linalg.solve(A[j], rhs[j])
                except np.linalg.LinAlgError:
                    pass
        out = {}
        for j in np.nonzero(np.isfinite(coef).all(axis=1))[0].tolist():
            out[int(ks[j])] = (int(x0[j]), int(y0[j]), int(x1[j]), int(y1[j]), tuple(float(c) for c in coef[j]))
        return out

    def _draw_floor(self, draw, cam):
        np, sc = self.np, self.scene
        step, span = 16.0, int(max(3, min(12, sc.radius * 1.5 // 16)))
        cx, cz = round(cam.target[0] / step) * step, round(cam.target[2] / step) * step
        segs = []
        for i in range(-span, span + 1):
            segs.append((cx + i * step, sc.floor_y, cz - span * step, cx + i * step, sc.floor_y, cz + span * step))
            segs.append((cx - span * step, sc.floor_y, cz + i * step, cx + span * step, sc.floor_y, cz + i * step))
        S = np.array(segs, dtype=float).reshape(-1, 3)
        sx, sy, _z, ok = self._project(cam, S)
        sx, sy, ok = sx.reshape(-1, 2), sy.reshape(-1, 2), ok.reshape(-1, 2).all(axis=1)
        for (xa, xb), (ya, yb) in zip(sx[ok].tolist(), sy[ok].tolist()):
            draw.line([xa, ya, xb, yb], fill=self.GRID)

    def _draw_bones(self, draw, cam, debug, selected):
        for node, (piv, tip, tgt, _rest) in debug.items():
            a, b, t = cam.project(piv), cam.project(tip), cam.project(tgt)
            if not (a and b):
                continue
            hot = node.name == selected
            draw.line([a[0], a[1], b[0], b[1]], fill=(251, 191, 36) if hot else (249, 115, 22), width=3 if hot else 1)
            draw.ellipse([b[0] - 3, b[1] - 3, b[0] + 3, b[1] + 3], fill=(253, 230, 138))
            if t:
                draw.ellipse([t[0] - 2, t[1] - 2, t[0] + 2, t[1] + 2], outline=(96, 165, 250))
            if hot:
                draw.text((b[0] + 8, b[1] - 5), node.name, fill=(253, 230, 138))

    # ----- the frame -------------------------------------------------------------------
    def render(self, cam, w, h, show_static=True, textured=True, floor=True, debug=None, selected=None, message=None):
        """-> PIL RGB image. `debug` = PhysSim.debug (bone lines), `message` = text for the top-left corner."""
        np = self.np
        w, h = max(int(w), 1), max(int(h), 1)
        img = self.Image.new("RGB", (w, h), self.BG)
        draw = self.ImageDraw.Draw(img)
        cam.setup(w, h)
        if floor:
            self._draw_floor(draw, cam)
        self.last_count = 0
        if len(self.fi):
            W = np.array([n.world for n in self.scene.nodes], dtype=float).reshape(-1, 3, 4)
            Wv = W[self.vnode]
            P = np.einsum("vij,vj->vi", Wv[:, :, :3], self.V) + Wv[:, :, 3]
            sx, sy, z, ok_v = self._project(cam, P)
            fi = self.fi
            A, B, C = P[fi[:, 0]], P[fi[:, 1]], P[fi[:, 2]]
            nrm = np.cross(B - A, C - A)
            ln = np.linalg.norm(nrm, axis=1)
            good = ln > 1e-12
            nrm = nrm / np.where(good, ln, 1.0)[:, None]
            back = ((np.array(cam.pos) - A) * nrm).sum(axis=1) <= 0
            nrm = np.where((back & self.ftwo)[:, None], -nrm, nrm)          # two-sided faces light from their visible side
            fsx0, fsy0 = sx[fi], sy[fi]
            on_screen = ~((fsx0.max(axis=1) < 0) | (fsx0.min(axis=1) > w) | (fsy0.max(axis=1) < 0) | (fsy0.min(axis=1) > h))
            vis = good & (~back | self.ftwo) & ok_v[fi].all(axis=1) & on_screen
            if not show_static:
                vis &= self.fphys
            sel = np.nonzero(vis)[0]
            if len(sel):
                lvl = (np.clip(nrm[sel] @ np.array(cam.light), 0.0, 1.0) * 15).astype(np.int64)
                cols = (self.fcol[sel] * (0.38 + 0.62 * lvl / 15.0)[:, None]).astype(np.uint8)
                order = np.argsort(-z[fi[sel]].mean(axis=1), kind="stable")        # painter's order: far first
                sel, lvl, cols = sel[order], lvl[order], cols[order]
                pts = np.stack([sx[fi[sel]], sy[fi[sel]]], axis=2).reshape(len(sel), 8).tolist()
                col_l = [tuple(c) for c in cols.tolist()]
                tex_ids = self.ftex[sel]
                quads = self._tex_quads(tex_ids, sx, sy, w, h) if (textured and (tex_ids >= 0).any()) else {}
                lvl_l, tid_l = lvl.tolist(), tex_ids.tolist()
                polygon, paste = draw.polygon, img.paste
                opaque = self.tex_opaque
                for k in range(len(sel)):
                    q = quads.get(k)
                    # solid under-fill hides hairline seams; faces with see-through texels must not get one
                    if q is None or opaque[tid_l[k]]:
                        polygon(pts[k], fill=col_l[k], outline=col_l[k])
                    if q is not None:
                        x0, y0, x1, y1, co = q
                        warped = self._shaded_tex(tid_l[k], lvl_l[k]).transform((x1 - x0, y1 - y0), self.T_PERSP, co, self.R_NEAREST)
                        paste(warped, (x0, y0), warped)
                self.last_count = len(sel)
        if debug:
            self._draw_bones(draw, cam, debug, selected)
        if message:
            draw.multiline_text((12, 12), message, fill=(252, 165, 165))
        return img


# ==========================================================================
# GUI
# ==========================================================================
class Tip:
    def __init__(self, widget, text):
        self.widget, self.text, self.tw = widget, text, None
        widget.bind("<Enter>", self.show, add="+")
        widget.bind("<Leave>", self.hide, add="+")

    def show(self, _=None):
        if self.tw or not self.text:
            return
        self.tw = tk.Toplevel(self.widget)
        self.tw.wm_overrideredirect(True)
        self.tw.wm_geometry(f"+{self.widget.winfo_rootx() + 16}+{self.widget.winfo_rooty() + self.widget.winfo_height() + 4}")
        tk.Label(self.tw, text=self.text, background="#ffffe0", foreground="#000000", relief="solid",
                 borderwidth=1, justify="left", wraplength=340, padx=6, pady=3).pack()

    def hide(self, _=None):
        if self.tw:
            self.tw.destroy()
            self.tw = None


class ScrollFrame(ttk.Frame):
    """Vertical scroll area; the scrollbar/wheel only act when the content is taller than the view."""

    def __init__(self, master):
        super().__init__(master)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        self.sb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self._sb_shown, self._job = False, None
        self.inner.bind("<Configure>", self._schedule)
        self.canvas.bind("<Configure>", self._schedule)
        self.canvas.configure(yscrollcommand=self.sb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.bind_all(seq, self._wheel, add="+")

    def _schedule(self, _=None):
        if self._job is None:
            self._job = self.after_idle(self._sync)

    def _sync(self):
        self._job = None
        if not self.winfo_exists():
            return
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        need = self.inner.winfo_reqheight() > ch > 1
        if need and not self._sb_shown:
            self.sb.pack(side="right", fill="y")
            self._sb_shown = True
        elif not need and self._sb_shown:
            self.sb.pack_forget()
            self._sb_shown = False
        self.canvas.itemconfigure(self._win, width=cw)
        self.canvas.configure(scrollregion=(0, 0, cw, max(self.inner.winfo_reqheight(), ch)))
        if not need:
            self.canvas.yview_moveto(0)

    def _wheel(self, e):
        try:
            w = self.winfo_containing(e.x_root, e.y_root)
        except (KeyError, tk.TclError):
            return
        while w is not None and w is not self:
            w = w.master
        if w is None or self.canvas.yview() == (0.0, 1.0):
            return
        if getattr(e, "num", None) == 4:
            step = -1
        elif getattr(e, "num", None) == 5:
            step = 1
        else:
            step = -1 if e.delta > 0 else 1
        self.canvas.yview_scroll(step, "units")


STATE_STYLE = {"edited": ("edited", "#d97706"), "script": ("script", "#6b7280"), "preset": ("preset", "#2563eb"),
               "expr": ("expression", "#b91c1c"), "default": ("default", "#9ca3af")}


class SliderRow:
    """label | slider | number box | source tag | reset | limiter (min to max)

    The limiter is the slider's allowed range: the track shrinks to it, typed values are clamped to it,
    and min = max locks the slider at that value."""

    def __init__(self, parent, row, label, lo, hi, step, integer, on_change, on_reset, tip="", indent=0, on_limits=None):
        self.lo, self.hi, self.step, self.integer = lo, hi, step, integer
        self.on_change, self.on_limits, self._guard = on_change, on_limits, False
        self.var = tk.StringVar()
        self.label = ttk.Label(parent, text=label)
        self.label.grid(row=row, column=0, sticky="w", padx=(6 + indent, 8), pady=1)
        self.scale = ttk.Scale(parent, from_=lo, to=hi, orient="horizontal", command=self._moved)
        self.scale.grid(row=row, column=1, sticky="ew", pady=1)
        self.entry = ttk.Entry(parent, textvariable=self.var, width=9, justify="right")
        self.entry.grid(row=row, column=2, padx=6, pady=1)
        self.src = ttk.Label(parent, width=10)
        self.src.grid(row=row, column=3, sticky="w")
        self.reset = None
        if on_reset:
            self.reset = ttk.Button(parent, text="\u21BA", width=3, command=on_reset)
            self.reset.grid(row=row, column=4, padx=(0, 6))
            Tip(self.reset, "Reset to the bone's preset / API default")
        self.entry.bind("<Return>", self._entered)
        self.entry.bind("<FocusOut>", self._entered)
        if tip:
            Tip(self.label, tip)
        # limiter
        self.min_var, self.max_var = tk.StringVar(), tk.StringVar()
        self.lim = ttk.Frame(parent)
        self.min_entry = ttk.Entry(self.lim, textvariable=self.min_var, width=6, justify="right")
        self.max_entry = ttk.Entry(self.lim, textvariable=self.max_var, width=6, justify="right")
        self.min_entry.pack(side="left")
        ttk.Label(self.lim, text=" to ").pack(side="left")
        self.max_entry.pack(side="left")
        self.lim.grid(row=row, column=5, padx=(0, 6))
        self.lim.grid_remove()
        for e in (self.min_entry, self.max_entry):
            e.bind("<Return>", self._limits_entered)
            e.bind("<FocusOut>", self._limits_entered)
        Tip(self.min_entry, "Lowest value this slider allows")
        Tip(self.max_entry, "Highest value this slider allows (same as the lowest = locked)")
        self.set_range(lo, hi)

    # ----- limiter ------------------------------------------------------------
    def show_limiters(self, show):
        if show:
            self.lim.grid()
        else:
            self.lim.grid_remove()

    def set_range(self, lo, hi):
        self.lo, self.hi = lo, hi
        self._guard = True
        try:
            self.scale.configure(from_=lo, to=hi)
        finally:
            self._guard = False
        self._show_range()
        locked = hi <= lo
        for w in (self.scale, self.entry):
            w.state(["disabled"] if locked else ["!disabled"])

    def _show_range(self):
        self.min_var.set(fmt_num(self.lo))
        self.max_var.set(fmt_num(self.hi))

    def _limits_entered(self, _=None):
        try:
            lo = float(self.min_var.get().replace(",", "."))
            hi = float(self.max_var.get().replace(",", "."))
        except ValueError:
            self._show_range()
            return
        if lo > hi:                                  # nonsense range: put the old one back
            self._show_range()
            return
        if (lo, hi) != (self.lo, self.hi) and self.on_limits:
            self.on_limits(lo, hi)

    # ----- value ------------------------------------------------------------------
    def clamp(self, v):
        return min(max(v, self.lo), self.hi)

    def _snap(self, v):
        v = round(v / self.step) * self.step
        return int(round(v)) if self.integer else round(v, 4)

    def _moved(self, raw):
        if self._guard:
            return
        v = self.clamp(self._snap(float(raw)))
        self.var.set(fmt_num(v))
        self.on_change(v)

    def _entered(self, _=None):
        try:
            v = float(self.var.get().replace(",", "."))
        except ValueError:
            return
        v = self.clamp(v)
        self._guard = True
        self.scale.set(v)
        self._guard = False
        self.var.set(fmt_num(v))
        self.on_change(v)

    def set(self, v, state):
        self._guard = True
        self.scale.set(self.clamp(v))
        self._guard = False
        self.var.set(fmt_num(v))
        txt, col = STATE_STYLE[state]
        self.src.configure(text=txt, foreground=col)


class LabeledScale(ttk.Frame):
    """Caption + slider + live value read-out."""

    def __init__(self, master, text, var, lo, hi, fmt, command=None):
        super().__init__(master)
        self.var, self.fmt = var, fmt
        ttk.Label(self, text=text, width=11).grid(row=0, column=0, sticky="w")
        self.scale = ttk.Scale(self, from_=lo, to=hi, variable=var, command=(lambda v: command()) if command else None)
        self.scale.grid(row=0, column=1, sticky="ew", padx=4)
        self.val = ttk.Label(self, width=6, anchor="e")
        self.val.grid(row=0, column=2)
        self.columnconfigure(1, weight=1)
        var.trace_add("write", self._show)
        self._show()

    def _show(self, *_):
        try:
            self.val.configure(text=self.fmt(float(self.var.get())))
        except (tk.TclError, ValueError):
            pass


NO_PHYS_MSG = ("No physBone groups found in this model, so nothing is simulated.\n"
               "Groups need a name starting with physBone / physBoob (or a setPreset id),\n"
               "or the same name as a bone in the bone list.")


class FrameSink:
    """Puts PIL images on a Tk canvas: Pillow's ImageTk if it works here, otherwise Tk's own PPM reader."""

    def __init__(self, canvas):
        self.canvas = canvas
        self.photo = self.item = self.size = None
        try:
            from PIL import ImageTk
            self._imagetk = ImageTk
        except Exception:
            self._imagetk = None

    def reset(self):                                  # forget the canvas item (after the canvas was cleared)
        self.photo = self.item = self.size = None

    def _new_photo(self, img):
        if self._imagetk is not None:
            try:
                return self._imagetk.PhotoImage(img)
            except Exception:
                self._imagetk = None
        return tk.PhotoImage(master=self.canvas, data=ppm_bytes(img), format="ppm")

    def show(self, img):
        if self.photo is None or self.size != img.size:
            self.photo, self.size = self._new_photo(img), img.size
            if self.item is None:
                self.item = self.canvas.create_image(0, 0, image=self.photo, anchor="nw")
            else:
                self.canvas.itemconfigure(self.item, image=self.photo)
        elif self._imagetk is not None:
            self.photo.paste(img)
        else:
            self.photo.configure(data=ppm_bytes(img), format="ppm")


class PreviewWindow(tk.Toplevel):
    """Live view of the .bbmodel with the physBones simulated from the editor's current values."""

    def __init__(self, app, scene, get_params, selected, notes="", use_fast=True):
        super().__init__(app)
        self.title(f"Live preview - {scene.name}")
        self.geometry("1120x740")
        self.scene, self.selected = scene, selected
        self.sim = PhysSim(scene, get_params)
        self.cam = Camera(scene.center, scene.radius * 3.2)
        self.fast, self.sink, self._mode, self.fast_error = None, None, None, ""
        if use_fast and render_libs() is not None:        # numpy + Pillow: much cheaper than canvas polygons
            try:
                self.fast = FastRenderer(scene)
            except Exception as e:
                self.fast_error = f"{type(e).__name__}: {e}"
        self.t, self.alive = 0.0, True
        self.grab = self.grab_target = (0.0, 0.0, 0.0)
        self._last = self._fps_t = self._deadline = time.perf_counter()
        self._frames, self._drag = 0, (0, 0)

        side = ttk.Frame(self, padding=8)
        side.pack(side="right", fill="y")
        main = ttk.Frame(self)
        main.pack(side="left", fill="both", expand=True)

        box = ttk.LabelFrame(side, text="Motion", padding=6)
        box.pack(fill="x")
        self.motion = tk.StringVar(value="Walk")
        ttk.Combobox(box, textvariable=self.motion, values=MOTIONS, state="readonly", width=22).pack(fill="x")
        self.slow = tk.DoubleVar(value=1.0)
        LabeledScale(box, "Speed", self.slow, 0.05, 2.0, lambda v: f"{v:.2f}x").pack(fill="x", pady=(4, 0))
        ttk.Label(box, text="Locked to 60 fps (the API's physics depends on frame rate).", wraplength=240,
                  justify="left", foreground="#777777").pack(anchor="w", pady=(6, 0))
        self.flip_dir = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text="walk the other way (if your model faces +Z)", variable=self.flip_dir).pack(anchor="w", pady=(4, 0))
        ttk.Button(box, text="Reset", command=self.reset).pack(anchor="e", pady=(4, 0))

        disp = ttk.LabelFrame(side, text="Display", padding=6)
        disp.pack(fill="x", pady=(8, 0))
        self.use_fast = tk.BooleanVar(value=self.fast is not None)
        fast_cb = ttk.Checkbutton(disp, text="Fast renderer (numpy + Pillow)", variable=self.use_fast)
        fast_cb.pack(anchor="w")
        if self.fast is None:
            fast_cb.state(["disabled"])
            Tip(fast_cb, "numpy/Pillow aren't available, so the basic renderer is used")
        self.show_tex = tk.BooleanVar(value=bool(scene.textures and scene.textures.usable()))
        ttk.Checkbutton(disp, text="Textures", variable=self.show_tex).pack(anchor="w")
        self.show_static = tk.BooleanVar(value=scene.n_faces < 4000)
        ttk.Checkbutton(disp, text="Static parts", variable=self.show_static).pack(anchor="w")
        self.show_debug = tk.BooleanVar(value=True)
        ttk.Checkbutton(disp, text="Bone lines", variable=self.show_debug).pack(anchor="w")

        ttk.Label(side, foreground="#777777", wraplength=250, justify="left", padding=(0, 8, 0, 0),
                  text="Drag the model to move it. Right-drag (or Shift/Ctrl+drag) orbits, wheel zooms.").pack(anchor="w")

        self.canvas = tk.Canvas(main, background="#20232a", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        if self.fast is not None:
            self.sink = FrameSink(self.canvas)
        self.info = tk.StringVar()
        ttk.Label(main, textvariable=self.info, foreground="#555555", padding=(6, 2)).pack(anchor="w")
        if notes:
            ttk.Label(main, text=notes, foreground="#b91c1c", padding=(6, 0), wraplength=800, justify="left").pack(anchor="w")
        ttk.Label(main, foreground="#8a6d3b", padding=(6, 0, 6, 6), wraplength=800, justify="left",
                  text="Runs a port of physBoneAPI.lua's own physics. Collisions (bounce, nodes, colliders) are not simulated. "
                       "A bone points the way its rotMod makes it: if a part sits wrong, adjust rotMod.").pack(anchor="w")
        c = self.canvas
        c.bind("<ButtonPress-1>", self._press)
        c.bind("<B1-Motion>", self._left_drag)
        for btn in ("2", "3"):
            c.bind(f"<ButtonPress-{btn}>", self._press)
            c.bind(f"<B{btn}-Motion>", self._orbit_drag)
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            c.bind(seq, self._wheel)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._tick()

    # ----- input ---------------------------------------------------------------
    def _press(self, e):
        self._drag = (e.x, e.y)

    def _orbit(self, dx, dy):
        self.cam.yaw -= dx * 0.01
        self.cam.pitch = max(-1.45, min(1.45, self.cam.pitch + dy * 0.01))

    def _orbit_drag(self, e):
        dx, dy = e.x - self._drag[0], e.y - self._drag[1]
        self._drag = (e.x, e.y)
        self._orbit(dx, dy)

    def _left_drag(self, e):
        if getattr(e, "state", 0) & 0x5:             # Shift or Ctrl held: orbit (for mice without a right button)
            self._orbit_drag(e)
            return
        dx, dy = e.x - self._drag[0], e.y - self._drag[1]
        self._drag = (e.x, e.y)
        k = self.cam.dist / max(self.cam.f, 1.0)
        self.grab_target = v_add(self.grab_target, v_add(v_mul(self.cam.right, dx * k), v_mul(self.cam.up, -dy * k)))

    def _wheel(self, e):
        up = getattr(e, "num", None) == 4 or getattr(e, "delta", 0) > 0
        self.cam.dist = max(self.scene.radius * 0.8, min(self.scene.radius * 20, self.cam.dist * (0.9 if up else 1.1)))

    def reset(self):
        self.t = 0.0
        self.grab = self.grab_target = (0.0, 0.0, 0.0)
        self.sim.reset()

    def close(self):
        self.alive = False
        self.destroy()

    # ----- loop / drawing --------------------------------------------------------
    def _tick(self):
        if not self.alive:
            return
        try:
            self._step()
        except Exception as e:                      # keep running and say what went wrong
            import traceback
            traceback.print_exc()
            self.info.set(f"Preview error: {type(e).__name__}: {e}")
        # pace the loop to exactly 60 fps: schedule against a running deadline so timer jitter doesn't accumulate
        self._deadline = max(self._deadline + FRAME_TIME, time.perf_counter() - FRAME_TIME)
        self.after(max(1, int((self._deadline - time.perf_counter()) * 1000)), self._tick)

    def _swing_text(self):
        parts = []
        for node, (piv, tip, _tgt, rest) in self.sim.debug.items():
            cs = max(-1.0, min(1.0, v_dot(v_norm(v_sub(tip, piv), rest), rest)))
            parts.append(f"{node.name} {math.degrees(math.acos(cs)):.0f}\u00b0")
        return "swing: " + ",  ".join(parts[:5]) + (" ..." if len(parts) > 5 else "") if parts else ""

    def _step(self):
        now = time.perf_counter()
        dt, self._last = min(now - self._last, 0.05), now
        slow = max(0.0, float(self.slow.get()))
        self.sim.fps = LOCKED_FPS
        self.t += dt * slow
        mode = self.motion.get()
        off, spin = motion_state(mode, self.t)
        if self.flip_dir.get():
            off = (off[0], off[1], -off[2])
        k = 1.0 - math.exp(-dt * 14.0)               # the grabbed model eases toward the mouse instead of teleporting
        self.grab = tuple(g + (tg - g) * k for g, tg in zip(self.grab, self.grab_target))
        ctr = self.scene.center
        self.cam.target = (ctr[0] + off[0], ctr[1], ctr[2] + off[2]) if mode in FOLLOW_MODES else tuple(ctr)
        pose = limb_pose(mode, self.t)
        if self.flip_dir.get():
            pose = {k: -v for k, v in pose.items()}
        self.sim.update(dt * slow, root_matrix(v_add(off, self.grab), spin, ctr), pose)
        n = self.draw()
        self._frames += 1
        if now - self._fps_t >= 0.5:
            fastmode = self._mode == "fast"
            self.info.set(f"{len(self.scene.phys_names)} physBone(s)  |  {self._frames / (now - self._fps_t):.0f} fps  |  "
                          f"{n} {'faces' if fastmode else 'polygons'} ({'fast' if fastmode else 'basic'} renderer)"
                          + ("  (turn off 'Static parts' if slow)" if n > (6000 if fastmode else 3000) else "")
                          + "  |  " + self._swing_text() + (f"  |  {self.fast_error}" if self.fast_error else ""))
            self._fps_t, self._frames = now, 0

    def _floor(self):
        cam, sc = self.cam, self.scene
        step, span = 16.0, int(max(3, min(12, sc.radius * 1.5 // 16)))
        cx, cz = round(cam.target[0] / step) * step, round(cam.target[2] / step) * step
        for i in range(-span, span + 1):
            for a, b in (((cx + i * step, sc.floor_y, cz - span * step), (cx + i * step, sc.floor_y, cz + span * step)),
                         ((cx - span * step, sc.floor_y, cz + i * step), (cx + span * step, sc.floor_y, cz + i * step))):
                pa, pb = cam.project(a), cam.project(b)
                if pa and pb:
                    self.canvas.create_line(pa[0], pa[1], pb[0], pb[1], fill="#2f3440")

    def _draw_basic(self, c, w, h):
        if self._mode == "fast" and self.sink is not None:
            self.sink.reset()
        self._mode = "basic"
        polys = render_scene(self.scene, self.cam, w, h, self.show_static.get(), self.show_tex.get())
        c.delete("all")
        self._floor()
        if not self.scene.phys_names:
            c.create_text(12, 12, anchor="nw", fill="#fca5a5", font=("TkDefaultFont", 11), text=NO_PHYS_MSG)
        for _, flat, col in polys:
            c.create_polygon(flat, fill=col, outline=col)
        if self.show_debug.get():
            sel = self.selected()
            for node, (piv, tip, tgt, _rest) in self.sim.debug.items():
                a, b, t = self.cam.project(piv), self.cam.project(tip), self.cam.project(tgt)
                if not (a and b):
                    continue
                hot = node.name == sel
                c.create_line(a[0], a[1], b[0], b[1], fill="#fbbf24" if hot else "#f97316", width=3 if hot else 1)
                c.create_oval(b[0] - 3, b[1] - 3, b[0] + 3, b[1] + 3, fill="#fde68a", outline="")
                if t:
                    c.create_oval(t[0] - 2, t[1] - 2, t[0] + 2, t[1] + 2, outline="#60a5fa")
                if hot:
                    c.create_text(b[0] + 8, b[1], text=node.name, anchor="w", fill="#fde68a")
        return len(polys)

    def _draw_fast(self, c, w, h):
        if self._mode != "fast":
            c.delete("all")
            self.sink.reset()
            self._mode = "fast"
        img = self.fast.render(self.cam, w, h, self.show_static.get(), self.show_tex.get(), True,
                               self.sim.debug if self.show_debug.get() else None, self.selected(),
                               None if self.scene.phys_names else NO_PHYS_MSG)
        self.sink.show(img)
        return self.fast.last_count

    def draw(self):
        c = self.canvas
        w, h = max(int(c.winfo_width()), 2), max(int(c.winfo_height()), 2)
        if self.fast is not None and self.sink is not None and self.use_fast.get():
            try:
                return self._draw_fast(c, w, h)
            except Exception as e:                       # never leave the user with a dead preview: fall back for good
                import traceback
                traceback.print_exc()
                self.fast_error = f"fast renderer failed ({type(e).__name__}: {e}); using the basic one"
                self.fast = self.sink = None
                self.use_fast.set(False)
        return self._draw_basic(c, w, h)


class App(tk.Tk):
    def __init__(self, path=None):
        super().__init__()
        self.title("Figura PhysBone Editor")
        self.geometry("1100x760")
        self.minsize(900, 560)
        self.settings = load_settings()
        self.preset_file = self.settings.get("presets", DEFAULT_PRESET_FILE)
        self.library = load_library(self.preset_file)
        self.doc = ScriptDoc()
        self.current = None
        self.preview_win = None
        self._libs_failed = False
        self.rows = {}
        self.slider_limits = self._load_slider_limits(self.settings.get("slider_limits"))
        self.iid_bone = {}
        self._building = False
        self.bold = tkfont.nametofont("TkDefaultFont").copy()
        self.bold.configure(weight="bold")

        top = ttk.Frame(self, padding=(8, 8, 8, 0))
        top.pack(fill="x")
        ttk.Label(top, text="Script:").pack(side="left")
        self.path_var = tk.StringVar()
        pe = ttk.Entry(top, textvariable=self.path_var)
        pe.pack(side="left", fill="x", expand=True, padx=6)
        pe.bind("<Return>", lambda e: self.load_script())
        ttk.Button(top, text="Browse\u2026", command=self.browse).pack(side="left")
        ttk.Button(top, text="Reload", command=self.load_script).pack(side="left", padx=(6, 0))

        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=8, pady=8)

        left = ttk.Frame(panes)
        panes.add(left, weight=1)
        ttk.Label(left, text="Bones", font=self.bold).pack(anchor="w")
        tf = ttk.Frame(left)
        tf.pack(fill="both", expand=True, pady=4)
        self.tree = ttk.Treeview(tf, columns=("n",), selectmode="extended")
        self.tree.heading("#0", text="Bone")
        self.tree.heading("n", text="In script")
        self.tree.column("#0", width=190)
        self.tree.column("n", width=62, anchor="center", stretch=False)
        tsb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tsb.pack(side="right", fill="y")
        self.tree.tag_configure("dirty", foreground="#d97706")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        b1 = ttk.Frame(left)
        b1.pack(fill="x")
        ttk.Button(b1, text="Add bone\u2026", command=self.add_bone).pack(side="left", padx=(0, 4))
        ttk.Button(b1, text="From .bbmodel\u2026", command=self.add_from_bbmodel).pack(side="left")
        b2 = ttk.Frame(left)
        b2.pack(fill="x", pady=(4, 0))
        ttk.Button(b2, text="Revert bone", command=self.revert_bone).pack(side="left", padx=(0, 4))
        ttk.Button(b2, text="All defaults", command=self.defaults_bone).pack(side="left")

        right = ttk.Frame(panes, padding=(10, 0, 0, 0))
        panes.add(right, weight=4)
        self.title_var = tk.StringVar(value="Open a script to begin")
        ttk.Label(right, textvariable=self.title_var, font=self.bold).pack(anchor="w")
        self.note_var = tk.StringVar()
        ttk.Label(right, textvariable=self.note_var, foreground="#b45309", wraplength=680, justify="left").pack(anchor="w")

        pb = ttk.LabelFrame(right, text="Presets", padding=6)
        pb.pack(fill="x", pady=(6, 6))
        r1 = ttk.Frame(pb)
        r1.pack(fill="x")
        self.preset_var = tk.StringVar()
        self.preset_box = ttk.Combobox(r1, textvariable=self.preset_var, state="readonly", width=28)
        self.preset_box.pack(side="left")
        ttk.Button(r1, text="Apply to bone", command=lambda: self.apply_preset(False)).pack(side="left", padx=(6, 0))
        ttk.Button(r1, text="Apply to selected", command=lambda: self.apply_preset(True)).pack(side="left", padx=(4, 0))
        ttk.Button(r1, text="Delete", command=self.delete_preset).pack(side="left", padx=(4, 0))
        r2 = ttk.Frame(pb)
        r2.pack(fill="x", pady=(6, 0))
        ttk.Button(r2, text="Save bone as preset\u2026", command=self.save_preset).pack(side="left")
        self.only_changed = tk.BooleanVar(value=False)
        ttk.Checkbutton(r2, text="only non-default values", variable=self.only_changed).pack(side="left", padx=8)
        ttk.Button(r2, text="Import\u2026", command=self.import_presets).pack(side="left", padx=(8, 0))
        ttk.Button(r2, text="Export\u2026", command=self.export_preset).pack(side="left", padx=(4, 0))
        ttk.Button(r2, text="Copy as setPreset()", command=self.copy_setpreset).pack(side="left", padx=(4, 0))

        lbar = ttk.Frame(right)
        lbar.pack(fill="x", pady=(0, 4))
        self.show_limiters = tk.BooleanVar(value=bool(self.settings.get("show_limiters", False)))
        ttk.Checkbutton(lbar, text="Slider limiters", variable=self.show_limiters, command=self._toggle_limiters).pack(side="left")
        self.lim_info = tk.StringVar()
        ttk.Label(lbar, textvariable=self.lim_info, foreground="#777777").pack(side="left", padx=8)
        ttk.Button(lbar, text="Reset limiters", command=self._reset_limiters).pack(side="right")

        self.scroll = ScrollFrame(right)
        self.scroll.pack(fill="both", expand=True)
        self._build_rows(self.scroll.inner)
        self._toggle_limiters()

        bottom = ttk.Frame(self, padding=(8, 0, 8, 8))
        bottom.pack(fill="x")
        ttk.Button(bottom, text="Preview changes\u2026", command=self.preview_changes).pack(side="left")
        ttk.Button(bottom, text="Save script  (Ctrl+S)", command=self.save_script).pack(side="left", padx=6)
        ttk.Button(bottom, text="Live preview\u2026", command=self.open_preview).pack(side="left")
        self.backup_var = tk.BooleanVar(value=self.settings.get("backup", True))
        ttk.Checkbutton(bottom, text="keep a .bak of the original", variable=self.backup_var).pack(side="left", padx=8)
        self.status_var = tk.StringVar()
        ttk.Label(bottom, textvariable=self.status_var, foreground="#555555").pack(side="right")

        self.bind("<Control-s>", lambda e: self.save_script())
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.refresh_presets()
        start = path or self.settings.get("script", "")
        if start and os.path.isfile(start):
            self.path_var.set(start)
            self.load_script()

    # ----- slider panel ---------------------------------------------------------
    def _build_rows(self, parent):
        parent.columnconfigure(0, weight=1)
        r = 0
        for g in GROUPS:
            lf = ttk.LabelFrame(parent, text=g, padding=(4, 4))
            lf.grid(row=r, column=0, sticky="ew", padx=2, pady=(0, 8))
            lf.columnconfigure(1, weight=1)
            r += 1
            gr = 0
            for p in [q for q in PARAMS if q.group == g]:
                if p.kind == "num":
                    lo, hi = self.limit_for(p.key, 0)
                    self.rows[(p.key, 0)] = SliderRow(
                        lf, gr, p.label, lo, hi, p.step, p.integer,
                        lambda v, k=p.key: self._changed(k, 0, v), lambda k=p.key: self._reset(k), p.hint,
                        on_limits=lambda a, b, k=p.key: self._limit_edited(k, 0, a, b))
                    gr += 1
                else:
                    hdr = ttk.Label(lf, text=p.label)
                    hdr.grid(row=gr, column=0, sticky="w", padx=6, pady=(4, 0))
                    Tip(hdr, p.hint)
                    gr += 1
                    for i, axis in enumerate("XYZ"):
                        lo, hi = self.limit_for(p.key, i)
                        self.rows[(p.key, i)] = SliderRow(
                            lf, gr, axis, lo, hi, p.step, False,
                            lambda v, k=p.key, i=i: self._changed(k, i, v),
                            (lambda k=p.key: self._reset(k)) if i == 0 else None, "", indent=18,
                            on_limits=lambda a, b, k=p.key, i=i: self._limit_edited(k, i, a, b))
                        gr += 1
        lf = ttk.LabelFrame(parent, text="Collider blacklist", padding=6)
        lf.grid(row=r, column=0, sticky="ew", padx=2, pady=(0, 8))
        lf.columnconfigure(0, weight=1)
        self.coll_text = tk.Text(lf, height=3, width=30, wrap="none", font="TkFixedFont", undo=True)
        self.coll_text.grid(row=0, column=0, sticky="ew")
        ttk.Label(lf, text="One collider ID per line - this bone ignores these colliders.",
                  foreground="#777777").grid(row=1, column=0, sticky="w")
        self.coll_text.bind("<KeyRelease>", self._colliders_changed)

    # ----- slider limiters ----------------------------------------------------------
    @staticmethod
    def _load_slider_limits(raw):
        out = {}
        if not isinstance(raw, dict):
            return out
        for key, axes in raw.items():
            p = PARAM_BY_KEY.get(key)
            n = 3 if (p and p.kind == "vec3") else 1
            if not p or not isinstance(axes, list) or len(axes) != n:
                continue
            clean = []
            for a in axes:
                ok = (isinstance(a, list) and len(a) == 2
                      and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in a) and a[0] <= a[1])
                clean.append([float(a[0]), float(a[1])] if ok else None)
            if any(clean):
                out[key] = clean
        return out

    def limit_for(self, key, idx):
        """(min, max) of one slider: the user's limiter if set, else the default range."""
        sl = self.slider_limits.get(key)
        if sl and idx < len(sl) and sl[idx]:
            return tuple(sl[idx])
        p = PARAM_BY_KEY[key]
        return (p.lo, p.hi)

    def _clamp_value(self, key, v):
        """Keep a value inside the user's limiters (a no-op for sliders without one)."""
        sl = self.slider_limits.get(key)
        if not sl:
            return v

        def one(x, i):
            a = sl[i] if i < len(sl) else None
            return min(max(x, a[0]), a[1]) if a else x
        if PARAM_BY_KEY[key].kind == "vec3":
            return [one(x, i) for i, x in enumerate(v)]
        return one(v, 0)

    def _limit_edited(self, key, idx, lo, hi):
        p = PARAM_BY_KEY[key]
        n = 3 if p.kind == "vec3" else 1
        sl = self.slider_limits.setdefault(key, [None] * n)
        sl[idx] = None if (lo == p.lo and hi == p.hi) else [lo, hi]
        if not any(sl):
            self.slider_limits.pop(key, None)
        self.settings["slider_limits"] = self.slider_limits
        self.rows[(key, idx)].set_range(lo, hi)
        self._update_lim_info()
        b = self.current
        if b:                                         # pull the bone's value into the new range right away
            cur = b.get(key)
            new = self._clamp_value(key, cur)
            if not same(new, cur):
                b.set(key, new)
                self._after_edit(key)
            else:
                self._show_param(key)

    def _toggle_limiters(self):
        show = bool(self.show_limiters.get())
        for row in self.rows.values():
            row.show_limiters(show)
        self.settings["show_limiters"] = show
        self._update_lim_info()

    def _update_lim_info(self):
        n = sum(1 for axes in self.slider_limits.values() for a in axes if a)
        if n:
            self.lim_info.set(f"{n} limiter(s) active")
        else:
            self.lim_info.set("type a min/max beside any slider (min = max locks it)" if self.show_limiters.get() else "")

    def _reset_limiters(self):
        if not self.slider_limits:
            return
        if not messagebox.askyesno("Reset limiters", "Remove all slider limiters?", parent=self):
            return
        self.slider_limits.clear()
        self.settings["slider_limits"] = self.slider_limits
        for (key, idx), row in self.rows.items():
            p = PARAM_BY_KEY[key]
            row.set_range(p.lo, p.hi)
        self._update_lim_info()
        if self.current:
            self.show_bone(self.current)

    def _changed(self, key, idx, v):
        if self._building or not self.current:
            return
        p = PARAM_BY_KEY[key]
        cur = self.current.get(key)
        if p.kind == "vec3":
            cur[idx] = v
        else:
            cur = v
        self.current.set(key, cur)
        self._after_edit(key)

    def _reset(self, key):
        if not self.current:
            return
        self.current.set(key, self._clamp_value(key, self.current.base(key)))
        self._after_edit(key)

    def _after_edit(self, key):
        self._show_param(key)
        self._update_row(self.current)

    def _show_param(self, key):
        p, b = PARAM_BY_KEY[key], self.current
        state, val = b.state(key), b.get(key)
        self._building = True
        try:
            if p.kind == "vec3":
                for i in range(3):
                    self.rows[(key, i)].set(val[i], state)
            else:
                self.rows[(key, 0)].set(val, state)
        finally:
            self._building = False

    def _colliders_changed(self, _=None):
        if self.current and not self._building:
            lines = [ln.strip() for ln in self.coll_text.get("1.0", "end-1c").splitlines() if ln.strip()]
            self.current.colliders = list(OrderedDict.fromkeys(lines))
            self._update_row(self.current)

    def show_bone(self, bone):
        self.current = bone
        self._building = True
        try:
            for p in PARAMS:
                self._building = False
                self._show_param(p.key)
            self._building = True
            self.coll_text.delete("1.0", "end")
            self.coll_text.insert("1.0", "\n".join(bone.colliders))
        finally:
            self._building = False
        self.title_var.set(f"Bone: {bone.name}")
        notes = []
        if bone.preset_id:
            notes.append(f"Created from the '{bone.preset_id}' preset: values tagged 'preset' come from it; "
                         "the script's own calls override them.")
        if bone.unparsed:
            notes.append("Not editable as numbers (script uses expressions): " +
                         ", ".join(f"{k} = {v}" for k, v in bone.unparsed.items()) + ". Moving that slider replaces the expression.")
        if bone.extra and not bone.calls:
            notes.append("No calls for this bone in the script yet; changed values will be added to events.entity_init().")
        self.note_var.set("\n".join(notes))

    # ----- bone list --------------------------------------------------------------
    def _refresh_tree(self, select=None):
        keep = select if select is not None else [self.iid_bone[i].name for i in self.tree.selection() if i in self.iid_bone]
        self.tree.delete(*self.tree.get_children())
        self.iid_bone = {}
        sel_ids = []
        for n, bone in enumerate(self.doc.bones.values()):
            iid = f"b{n}"
            self.iid_bone[iid] = bone
            self.tree.insert("", "end", iid=iid, text=bone.name + ("  *" if bone.dirty() else ""),
                             values=(bone.script_count(),), tags=("dirty",) if bone.dirty() else ())
            if bone.name in keep:
                sel_ids.append(iid)
        if sel_ids:
            self.tree.selection_set(sel_ids)

    def _update_row(self, bone):
        """Update one bone's row in place (cheap enough to call while dragging a slider)."""
        for iid, b in self.iid_bone.items():
            if b is bone:
                d = bone.dirty()
                self.tree.item(iid, text=bone.name + ("  *" if d else ""), values=(bone.script_count(),),
                               tags=("dirty",) if d else ())
                return

    def on_select(self, _=None):
        sel = [self.iid_bone[i] for i in self.tree.selection() if i in self.iid_bone]
        if sel and sel[0] is not self.current:
            self.show_bone(sel[0])

    def selected_bones(self):
        return [self.iid_bone[i] for i in self.tree.selection() if i in self.iid_bone]

    # ----- script I/O -----------------------------------------------------------------
    def browse(self):
        cur = self.path_var.get().strip()
        start = os.path.dirname(cur) if cur and os.path.isdir(os.path.dirname(cur)) else guess_avatar_dir()
        p = filedialog.askopenfilename(initialdir=start, title="Select your avatar's script.lua",
                                       filetypes=[("Lua scripts", "*.lua"), ("All files", "*.*")], parent=self)
        if p:
            self.path_var.set(p)
            self.load_script()

    def _confirm_discard(self):
        return (not self.doc.dirty()) or messagebox.askyesno("Unsaved changes", "Discard your unsaved edits?", parent=self)

    def load_script(self, keep_selection=None):
        path = self.path_var.get().strip()
        if not path:
            return
        if not os.path.isfile(path):
            messagebox.showerror("Script", f"Can't find:\n{path}", parent=self)
            return
        if keep_selection is None and not self._confirm_discard():
            return
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError) as e:
            messagebox.showerror("Script", f"Couldn't read the file:\n{e}", parent=self)
            return
        extra = [b.name for b in self.doc.bones.values() if b.extra] if self.doc.path == path else []
        self.doc = ScriptDoc(path, text, extra)
        self.doc.mtime = os.path.getmtime(path)
        self.settings["script"] = path
        self.current = None
        self.refresh_presets()
        self._refresh_tree(select=keep_selection or [])
        if not self.tree.selection() and self.doc.bones:
            first = next(iter(self.iid_bone))
            self.tree.selection_set(first)
        n = len(self.doc.bones)
        warn = "" if self.doc.has_require else "   (no require('physBoneAPI') found - it will be added on save)"
        self.status_var.set(f"{n} bone(s), {len(self.doc.script_presets)} script preset(s){warn}")
        if not n:
            self.title_var.set("No physBone calls found - use 'Add bone...' or 'From .bbmodel...'")

    def preview_changes(self):
        if not self.doc.path:
            return
        new, notes = self.doc.build_new_text()
        if new == self.doc.text:
            messagebox.showinfo("Preview", "No changes to write.", parent=self)
            return
        diff = "".join(difflib.unified_diff(self.doc.text.splitlines(True), new.splitlines(True),
                                            "script.lua (current)", "script.lua (after save)"))
        win = tk.Toplevel(self)
        win.title("Changes that will be written")
        win.geometry("820x520")
        txt = tk.Text(win, wrap="none", font="TkFixedFont")
        sb = ttk.Scrollbar(win, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(fill="both", expand=True)
        txt.tag_configure("add", foreground="#15803d")
        txt.tag_configure("del", foreground="#b91c1c")
        for line in diff.splitlines(True):
            tag = "add" if line.startswith("+") and not line.startswith("+++") else \
                "del" if line.startswith("-") and not line.startswith("---") else ""
            txt.insert("end", line, tag)
        txt.configure(state="disabled")

    def save_script(self):
        path = self.doc.path
        if not path:
            return
        new, notes = self.doc.build_new_text()
        if new == self.doc.text:
            self.status_var.set("Nothing to save")
            return
        try:
            if os.path.getmtime(path) != self.doc.mtime and not messagebox.askyesno(
                    "File changed", "The script was modified outside this editor since you opened it.\n"
                                    "Overwrite it anyway?", icon="warning", default="no", parent=self):
                return
            if self.backup_var.get() and not os.path.exists(path + ".bak"):
                shutil.copy2(path, path + ".bak")
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="") as fh:
                fh.write(new)
            os.replace(tmp, path)
        except OSError as e:
            messagebox.showerror("Save failed", str(e), parent=self)
            return
        sel = [b.name for b in self.selected_bones()]
        self.settings["backup"] = bool(self.backup_var.get())
        self.load_script(keep_selection=sel)
        self.status_var.set(f"Saved {len(notes)} change(s) to {os.path.basename(path)}")

    # ----- bone actions ----------------------------------------------------------------
    def add_bone(self):
        name = simpledialog.askstring("Add bone", "Bone name (the Blockbench group name, e.g. physBoneHair):", parent=self)
        if name and name.strip():
            name = name.strip()
            self.doc.ensure_bone(name)
            self._refresh_tree(select=[name])
            self.show_bone(self.doc.bones[name])

    def add_from_bbmodel(self):
        cur = self.path_var.get().strip()
        start = os.path.dirname(cur) if cur else guess_avatar_dir()
        p = filedialog.askopenfilename(initialdir=start, title="Select your .bbmodel",
                                       filetypes=[("Blockbench model", "*.bbmodel"), ("All files", "*.*")], parent=self)
        if not p:
            return
        try:
            names = bbmodel_bones(p, list(self.doc.script_presets))
        except (OSError, ValueError) as e:
            messagebox.showerror("Model", f"Couldn't read the model:\n{e}", parent=self)
            return
        new = [n for n in names if n not in self.doc.bones]
        for n in new:
            self.doc.ensure_bone(n)
        self._refresh_tree(select=[n for n in names])
        self.status_var.set(f"Found {len(names)} physBone group(s), {len(new)} new")

    def revert_bone(self):
        b = self.current
        if b:
            b.values.clear()
            b.touched.clear()
            b.colliders = list(b.orig_colliders)
            self.show_bone(b)
            self._refresh_tree()

    def defaults_bone(self):
        b = self.current
        if b:
            for p in PARAMS:
                b.set(p.key, self._clamp_value(p.key, b.base(p.key)))
            self.show_bone(b)
            self._refresh_tree()

    # ----- presets -------------------------------------------------------------------------
    def all_presets(self):
        out = OrderedDict()
        for name, v in BUILTIN_PRESETS.items():
            out[f"[built-in] {name}"] = v
        for name, v in self.doc.script_presets.items():
            out[f"[script] {name}"] = v
        out.update(self.library)
        return out

    def refresh_presets(self, select=None):
        names = list(self.all_presets())
        self.preset_box.configure(values=names)
        self.preset_var.set(select if select in names else (self.preset_var.get() if self.preset_var.get() in names else ""))

    def _chosen_preset(self):
        name = self.preset_var.get()
        vals = self.all_presets().get(name)
        if vals is None:
            messagebox.showinfo("Preset", "Choose a preset first.", parent=self)
        return name, vals

    def apply_preset(self, to_selected):
        name, vals = self._chosen_preset()
        if vals is None:
            return
        bones = self.selected_bones() if to_selected else ([self.current] if self.current else [])
        if not bones:
            messagebox.showinfo("Preset", "Select a bone first.", parent=self)
            return
        for b in bones:
            for k, v in vals.items():
                b.set(k, self._clamp_value(k, v))
        if self.current:
            self.show_bone(self.current)
        self._refresh_tree()
        self.status_var.set(f"Applied '{name}' to {len(bones)} bone(s) (not saved yet)")

    def save_preset(self):
        b = self.current
        if not b:
            return
        name = simpledialog.askstring("Save preset", "Preset name:", initialvalue=b.name, parent=self)
        if not name or not name.strip():
            return
        name = name.strip()
        if name in self.library and not messagebox.askyesno("Preset", f"Overwrite '{name}'?", parent=self):
            return
        vals = {}
        for p in PARAMS:
            v = b.get(p.key)
            if self.only_changed.get() and same(v, default_value(p)):
                continue
            vals[p.key] = v
        self.library[name] = vals
        self._write_library()
        self.refresh_presets(select=name)
        self.status_var.set(f"Saved preset '{name}'")

    def delete_preset(self):
        name = self.preset_var.get()
        if name not in self.library:
            messagebox.showinfo("Preset", "Only your own saved presets can be deleted (not [built-in] or [script] ones).", parent=self)
            return
        if messagebox.askyesno("Delete preset", f"Delete '{name}'?", parent=self):
            del self.library[name]
            self._write_library()
            self.refresh_presets()

    def _write_library(self):
        try:
            save_library(self.preset_file, self.library)
        except OSError as e:
            messagebox.showerror("Presets", f"Couldn't write {self.preset_file}:\n{e}", parent=self)

    def export_preset(self):
        name, vals = self._chosen_preset()
        if vals is None:
            return
        p = filedialog.asksaveasfilename(defaultextension=".json", initialfile=re.sub(r"[^\w.-]+", "_", name) + ".json",
                                         filetypes=[("Preset", "*.json")], parent=self)
        if p:
            with open(p, "w", encoding="utf-8") as fh:
                json.dump({"presets": {re.sub(r"^\[(script|built-in)\] ", "", name): vals}}, fh, indent=2)
            self.status_var.set(f"Exported '{name}'")

    def import_presets(self):
        p = filedialog.askopenfilename(filetypes=[("Preset", "*.json"), ("All files", "*.*")], parent=self)
        if not p:
            return
        imported = load_library(p)
        if not imported:
            messagebox.showinfo("Import", "No presets found in that file.", parent=self)
            return
        self.library.update(imported)
        self._write_library()
        self.refresh_presets(select=next(iter(imported)))
        self.status_var.set(f"Imported {len(imported)} preset(s)")

    def copy_setpreset(self):
        b = self.current
        if not b:
            return
        name = simpledialog.askstring("setPreset", "Preset ID (also the Blockbench group prefix):", initialvalue="physMyPreset", parent=self)
        if not name:
            return
        line = make_setpreset(name.strip(), {p.key: b.get(p.key) for p in PARAMS}, self.doc.pb_var)
        self.clipboard_clear()
        self.clipboard_append(line)
        self.status_var.set("Copied. Paste it above events.entity_init() in your script.")

    # ----- live preview ----------------------------------------------------------------------
    def sim_params(self, name):
        b = self.doc.bones.get(name)
        return {p.key: (b.get(p.key) if b else default_value(p)) for p in PARAMS}

    def _prepare_render_libs(self):
        """True when the fast renderer is usable. The first time, numpy + Pillow are installed (into a private
        folder, never the system Python) while a small window says what is happening."""
        if render_libs() is not None:
            return True
        if self._libs_failed:
            return False
        box = tk.Toplevel(self)
        box.title("Faster preview")
        ttk.Label(box, justify="center", padding=24,
                  text="Installing numpy and Pillow for a faster preview...\nThis happens once and can take a minute.").pack()
        try:
            box.update()
        except tk.TclError:
            pass
        result = {}
        th = threading.Thread(target=lambda: result.update(r=install_render_libs()), daemon=True)
        th.start()
        while th.is_alive():
            try:
                self.update()                              # keep the window alive while pip works
            except tk.TclError:
                break
            time.sleep(0.03)
        try:
            box.destroy()
        except tk.TclError:
            pass
        ok, msg = result.get("r", (False, "the install was interrupted"))
        if ok:
            self.status_var.set("Installed numpy and Pillow: using the fast preview renderer")
            return True
        self._libs_failed = True
        messagebox.showwarning("Faster preview", "Couldn't install numpy and Pillow automatically, so the preview uses the "
                               "slower built-in renderer.\n\n" + msg[-500:] + "\n\nTo get the fast renderer yourself, run:\n    "
                               + sys.executable + " -m pip install numpy pillow", parent=self)
        return False

    def _find_bbmodel(self):
        saved = self.settings.get("bbmodel", "")
        if saved and os.path.isfile(saved):
            return saved
        base = os.path.dirname(self.doc.path) if self.doc.path else ""
        found = []
        for folder in (base, os.path.join(base, "models")):
            if folder and os.path.isdir(folder):
                found += [os.path.join(folder, f) for f in sorted(os.listdir(folder)) if f.lower().endswith(".bbmodel")]
        return found[0] if len(found) == 1 else ""

    def open_preview(self):
        path = self._find_bbmodel()
        if not path:
            start = os.path.dirname(self.doc.path) if self.doc.path else guess_avatar_dir()
            path = filedialog.askopenfilename(initialdir=start, title="Select your avatar's .bbmodel",
                                              filetypes=[("Blockbench model", "*.bbmodel"), ("All files", "*.*")], parent=self)
        if not path:
            return
        try:
            scene = load_scene(path, tuple(BONE_PREFIXES) + tuple(self.doc.script_presets), names=set(self.doc.bones))
        except (OSError, ValueError, KeyError, TypeError) as e:
            messagebox.showerror("Live preview", f"Couldn't read the model:\n{e}", parent=self)
            return
        if not scene.n_elems:
            messagebox.showinfo("Live preview", "That model has no cubes or meshes to draw.", parent=self)
            return
        self.settings["bbmodel"] = path
        new = [n for n in scene.phys_names if n not in self.doc.bones]
        for n in new:
            self.doc.ensure_bone(n)
        if new:
            self._refresh_tree()
        if self.preview_win is not None and self.preview_win.alive:
            self.preview_win.close()
        missing = [n for n in self.doc.bones if n not in scene.phys_names]
        notes = ("Bones in the list that have no matching group in this model (not simulated): " + ", ".join(missing[:8])
                 + (" ..." if len(missing) > 8 else "")) if missing else ""
        notes = " ".join(x for x in (notes, scene.tex_note) if x)
        use_fast = self._prepare_render_libs()
        self.preview_win = PreviewWindow(self, scene, self.sim_params,
                                         lambda: self.current.name if self.current else None, notes, use_fast)
        self.status_var.set(f"Preview: {scene.n_elems} parts, {len(scene.phys_names)} physBone group(s)"
                            + (f" ({len(new)} added to the bone list)" if new else ""))

    # ----- closing ------------------------------------------------------------------------
    def on_close(self):
        if self.doc.dirty():
            ans = messagebox.askyesnocancel("Unsaved changes", "Save your changes before closing?", parent=self)
            if ans is None:
                return
            if ans:
                self.save_script()
                if self.doc.dirty():
                    return
        self.settings["backup"] = bool(self.backup_var.get())
        self.settings["slider_limits"] = self.slider_limits
        save_settings(self.settings)
        if self.preview_win is not None and self.preview_win.alive:
            self.preview_win.close()
        self.destroy()


if __name__ == "__main__":
    App(sys.argv[1] if len(sys.argv) > 1 else None).mainloop()
