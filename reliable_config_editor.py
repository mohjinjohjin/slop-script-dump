#!/usr/bin/env python3
"""
Reliable Config Editor
======================
A small GUI for editing the JSON rule files used by two Minecraft mods:

  * Reliable Recipes  -> <instance>/config/reliable_recipes/*.json
  * Reliable Remover  -> <instance>/config/reliable_remover/*.json

Tab 1 edits recipe rules (remove_recipe, replace_input, replace_output,
add_recipe, tag changes, repair rules ...). Tab 2 edits removal rules
(remove, remove_attacks, remove_interactions, remove_trade, ...).

Pick your Minecraft instance folder (or the config folder, or the
reliable_* folder itself) and press Load. Rules are listed per file; select
one to edit it with a form. Anything the form doesn't know about is kept in
the "Other fields (JSON)" box, and the "Raw JSON" tab lets you edit the whole
rule by hand, so nothing is ever lost. The "Preview" tab draws the rule (crafting
grids, find -> replace arrows, red X marks for removals) and its slots are clickable:
type an item ID into a slot and the rule updates live.

Requires Python 3.8+ with tkinter (Linux: `sudo apt install python3-tk`).
No other dependencies; if tkinter is missing the script offers to install it.   Run:  python reliable_config_editor.py
"""

import colorsys
import copy
import hashlib
import importlib
import json
import os
import re
import shutil
import subprocess
import sys


def ensure_dependencies():
    """The only dependency is tkinter, which ships with Python but is a separate
    system package on some platforms. pip can't install it, so this uses the OS
    package manager (asking first) and then retries the import."""
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
            print("Windows: re-run the Python installer, choose Modify, and tick "
                  "'tcl/tk and IDLE'. Then run this script again.")
        else:
            print("Please install tkinter for your Python (package is usually python3-tk) "
                  "and run this script again.")
        sys.exit(1)

    print("I can install it with:  " + " ".join(cmd))
    if sys.stdin and sys.stdin.isatty():
        if input("Run that now? [Y/n] ").strip().lower() in ("n", "no"):
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
        print("tkinter still isn't available to this Python (virtualenv or a different "
              "interpreter?). Run the script with the system Python, e.g. /usr/bin/python3.")
        sys.exit(1)
    print("tkinter installed.\n")


ensure_dependencies()

import tkinter as tk  # noqa: E402
from tkinter import filedialog, messagebox, simpledialog, ttk  # noqa: E402
from tkinter import font as tkfont  # noqa: E402

SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".reliable_config_editor.json")


# --------------------------------------------------------------------------
# Profiles: what each mod's rules look like
# --------------------------------------------------------------------------
class Field:
    def __init__(self, key, label, kind="list", hint="", template=""):
        self.key = key
        self.label = label
        self.kind = kind          # "str" | "list" (one value per line) | "json"
        self.hint = hint
        self.template = template  # inserted when the field is empty after an action change


class Profile:
    def __init__(self, key, tab_title, folder_name, fields_by_action, default_fields,
                 aliases=None, collapse_single=True, blank_action="", required=None,
                 filter_keys=(), needs_filter=(), no_filter_msg="", new_file_name="changes.json",
                 raw_recipe_ok=False, primary=None):
        self.key = key
        self.tab_title = tab_title
        self.folder_name = folder_name
        self.fields_by_action = fields_by_action
        self.default_fields = default_fields
        self.aliases = aliases or {}
        self.collapse_single = collapse_single   # write one-item lists as a plain string
        self.blank_action = blank_action
        self.required = required or {}
        self.filter_keys = tuple(filter_keys)
        self.needs_filter = set(needs_filter)
        self.no_filter_msg = no_filter_msg
        self.new_file_name = new_file_name
        self.raw_recipe_ok = raw_recipe_ok
        self.primary = primary or {}   # action -> how many leading fields the action uses
        self.action_names = list(fields_by_action.keys())

    def canonical(self, action):
        return self.aliases.get(action, action)

    def field_kind(self, key):
        for fields in list(self.fields_by_action.values()) + [self.default_fields]:
            for f in fields:
                if f.key == key:
                    return f.kind
        return "list"

    def fields_for(self, action):
        return self.fields_by_action.get(self.canonical(action), self.default_fields)

    def blank_rule(self):
        return {"action": self.blank_action} if self.blank_action else {}

    def validate(self, rule):
        action = rule.get("action")
        if not action:
            if self.raw_recipe_ok and "type" in rule:
                return []  # standalone recipe JSON, no action needed
            return ["No action set."]
        if not isinstance(action, str):
            return ["'action' must be a string."]
        canon = self.canonical(action)
        msgs = []
        if canon not in self.fields_by_action:
            msgs.append(f"Unknown action '{action}' - the mod may ignore it.")
            return msgs
        for group in self.required.get(canon, []):
            if not any(k in rule for k in group):
                msgs.append(f"'{canon}' needs: {' or '.join(group)}.")
        if canon in self.needs_filter and not any(k in rule for k in self.filter_keys):
            msgs.append(self.no_filter_msg)
        return msgs


RECIPE_TEMPLATE = """{
  "type": "minecraft:crafting_shapeless",
  "ingredients": [
    { "item": "minecraft:dirt" }
  ],
  "result": { "id": "minecraft:stick", "count": 4 }
}"""

# Every recipe action shows the same boxes (like the Remover tab). The boxes the
# chosen action actually uses come first; the rest follow under a divider.
_R_FIELDS = {f.key: f for f in [
    Field("target", "Item(s) to find", "list", "replace_input: item/#tag to replace  |  repair rules: the tool"),
    Field("replacement", "Replace with", "list", "replace_input: item/#tag(s) to use  |  replace_output: new output"),
    Field("material", "Repair material", "list", "set_repair_material: item/#tag accepted in the anvil"),
    Field("tag", "Tag", "list", "remove_from_tag: e.g. minecraft:planks"),
    Field("tags", "Tags", "list", "clear_tag: tags to empty, e.g. curios:artifact"),
    Field("id", "Recipe ID / items", "list", "Recipe ID filter  |  items for remove_all_tags / remove_from_tag"),
    Field("output", "Output item", "list", "Item the recipe produces, e.g. minecraft:stone_pickaxe"),
    Field("mod", "Mod ID", "list", "Mod that owns the recipe, e.g. farmersdelight"),
    Field("type", "Recipe type", "list", "e.g. minecraft:smoking"),
    Field("input", "Has ingredient", "list", "Recipe uses this item or #tag"),
]}
_R_FILTERS = ["output", "id", "mod", "type", "input"]
_R_PRIMARY = {
    "remove_recipe": _R_FILTERS,
    "replace_input": ["target", "replacement"] + _R_FILTERS,
    "replace_output": ["replacement"] + _R_FILTERS,
    "prevent_repair": ["target"],
    "set_repair_material": ["target", "material"],
    "remove_all_tags": ["id"],
    "remove_from_tag": ["tag", "id"],
    "clear_tag": ["tags"],
}


def _recipe_fields(action):
    first = _R_PRIMARY[action]
    return [_R_FIELDS[k] for k in first] + [f for k, f in _R_FIELDS.items() if k not in first]


_R_ACTIONS = ["remove_recipe", "replace_input", "replace_output", "prevent_repair",
              "set_repair_material", "add_recipe", "remove_all_tags", "remove_from_tag", "clear_tag"]
_R_BY_ACTION = {a: (_recipe_fields(a) if a in _R_PRIMARY else [
    Field("id", "Recipe ID", "list", "Optional, e.g. reliable_recipes:dirt_to_stick"),
    Field("recipe", "Recipe (JSON)", "json",
          "Standard recipe object (template is 1.21.1-style; adjust to your version)",
          template=RECIPE_TEMPLATE),
]) for a in _R_ACTIONS}

RECIPES = Profile(
    key="recipes",
    tab_title="Recipes  (Reliable Recipes)",
    folder_name="reliable_recipes",
    fields_by_action=_R_BY_ACTION,
    default_fields=[],
    aliases={"remove": "remove_recipe", "add": "add_recipe", "remove_tag": "remove_all_tags"},
    collapse_single=True,
    blank_action="remove_recipe",
    required={
        "replace_input": [("target",), ("replacement",)],
        "replace_output": [("replacement",)],
        "prevent_repair": [("target",)],
        "set_repair_material": [("target",), ("material",)],
        "add_recipe": [("recipe",)],
        "remove_all_tags": [("id",)],
        "remove_from_tag": [("tag",), ("id",)],
        "clear_tag": [("tags",)],
    },
    filter_keys=_R_FILTERS,
    needs_filter={"remove_recipe", "replace_output"},
    no_filter_msg="No filters set - this rule applies to EVERY recipe.",
    new_file_name="my_recipe_changes.json",
    raw_recipe_ok=True,
    primary={a: len(v) for a, v in _R_PRIMARY.items()},
)

_ITEMS = Field("items", "Items", "list", "Item IDs or #tags, e.g. minecraft:tnt, #minecraft:logs")
_FLUIDS = Field("fluids", "Fluids", "list", "Fluid IDs or #tags, e.g. minecraft:lava")
_MOD = Field("mod", "Mod ID(s)", "list", "Everything from a mod, e.g. farmersdelight")
_PATTERN = Field("pattern", "Regex pattern", "str", "Matched against IDs, e.g. /.*_sword/")
_DIMS = Field("dimensions", "Only in dimensions", "list", "e.g. minecraft:the_nether")
_COMPONENTS = Field("components", "Item components (1.21+)", "list", "e.g. [damage=0]")
_NBT = Field("nbt", "Item NBT (1.20.1)", "list", "e.g. {Damage:0}")
_NOT = Field("not", "Exceptions (JSON)", "json", 'Filters to exclude, e.g. {"items": ["minecraft:golden_pickaxe"]}')
_ENTITIES = Field("entities", "Entities", "list", "e.g. minecraft:villager")
_TAG_TYPE = Field("tag_type", "Tag type", "list", 'Only strip from "item" and/or "block" tags')
_REPLACE_WITH = Field("replace_with", "Replace with", "str", "Swap the item for this one in recipes/outputs")
_EFFECTS = Field("effects", "Effects", "list", "e.g. minecraft:poison")
_ENCHANTS = Field("enchantments", "Enchantments", "list", "e.g. minecraft:mending, #minecraft:curse")

_CORE = [_ITEMS, _FLUIDS, _MOD, _PATTERN, _DIMS, _COMPONENTS, _NBT, _NOT]

REMOVER = Profile(
    key="remover",
    tab_title="Remover  (Reliable Remover)",
    folder_name="reliable_remover",
    fields_by_action={
        "remove": _CORE + [_ENTITIES, _TAG_TYPE, _REPLACE_WITH],
        "remove_creative": _CORE,
        "remove_inventory": _CORE,
        "remove_drops": _CORE,
        "remove_storage": _CORE,
        "remove_equipment": _CORE + [_ENTITIES],
        "remove_chest_loot": _CORE,
        "remove_interactions": _CORE,
        "remove_attacks": _CORE + [_ENTITIES],
        "remove_enchantment": [_ENCHANTS] + _CORE,
        "remove_effect": [_EFFECTS, _DIMS, _NOT],
        "remove_potion": [_EFFECTS, _DIMS, _NOT],
        "remove_hand_swing": _CORE,
        "remove_info": _CORE,
        "remove_trade": _CORE,
        "remove_loot": [_ENCHANTS] + _CORE,
    },
    default_fields=_CORE,
    collapse_single=False,
    blank_action="remove",
    required={
        "remove_enchantment": [("enchantments",)],
        "remove_effect": [("effects",)],
        "remove_potion": [("effects",)],
    },
    filter_keys=["items", "fluids", "mod", "pattern", "patterns", "entities", "components", "nbt"],
    needs_filter={"remove"},
    no_filter_msg="No filter set (items, mod, pattern...) - this rule may match nothing, or everything.",
    new_file_name="removals.json",
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _brief_recipe(rec):
    typ = str(rec.get("type", "?")).split(":")[-1]
    opts, count = stack(rec["result"] if "result" in rec else rec.get("output"))
    res = opts[0][1] if opts else "?"
    return f"{typ} \u2192 {res}" + (f" \u00d7{count}" if isinstance(count, int) and count > 1 else "")


def summarize(rule, limit=110):
    parts = []
    skip = {"action"}
    if "target" in rule and "replacement" in rule:   # replace_input / replace_output style
        a = ", ".join(vals(rule["target"])) or "?"
        b = ", ".join(vals(rule["replacement"])) or "?"
        parts.append(f"{a} \u2192 {b}")
        skip |= {"target", "replacement"}
    for k, v in rule.items():
        if k in skip:
            continue
        if k == "recipe" and isinstance(v, dict):
            parts.append(_brief_recipe(v))
            continue
        if isinstance(v, str):
            s = v
        elif isinstance(v, list):
            s = ", ".join(x if isinstance(x, str) else json.dumps(x, ensure_ascii=False) for x in v)
        else:
            s = json.dumps(v, ensure_ascii=False)
        parts.append(f"{k}: {s}")
    text = "   |   ".join(parts)
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def resolve_folder(path, name):
    """Turn whatever the user picked into the real <...>/reliable_xxx folder."""
    if not path:
        return ""
    path = os.path.abspath(os.path.expanduser(path))
    probe = path
    while True:  # picked the folder itself, or a subfolder of it
        if os.path.basename(probe).lower() == name:
            return probe
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    for cand in (os.path.join(path, "config", name), os.path.join(path, name)):
        if os.path.isdir(cand):
            return cand
    if os.path.isdir(os.path.join(path, "config")):
        return os.path.join(path, "config", name)
    return os.path.join(path, name)


def load_settings():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except OSError:
        pass


class FileState:
    def __init__(self, path, rules, single, error=None, new=False):
        self.path = path
        self.rules = rules        # list of dicts
        self.single = single      # file originally held one bare object (e.g. a raw recipe)
        self.error = error
        self.dirty = new


# --------------------------------------------------------------------------
# Scrollable frame
# --------------------------------------------------------------------------
class ScrollFrame(ttk.Frame):
    """Vertical scroll area. The scrollbar only appears (and the mouse wheel only
    scrolls the page) when the content is taller than the visible area."""

    def __init__(self, master):
        super().__init__(master)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        self.sb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self._sb_shown = False
        self._sync_job = None
        self.inner.bind("<Configure>", self._schedule_sync)
        self.canvas.bind("<Configure>", self._schedule_sync)
        self.canvas.configure(yscrollcommand=self.sb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.bind_all(seq, self._wheel, add="+")

    def _schedule_sync(self, _=None):
        if self._sync_job is None:
            self._sync_job = self.after_idle(self._sync)

    def _sync(self):
        self._sync_job = None
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
        # never let the scroll region be shorter than the view, so short forms can't drift
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
        if w is None:
            return
        if self.canvas.yview() == (0.0, 1.0):      # everything already visible
            return
        if getattr(e, "num", None) == 4:
            step = -1
        elif getattr(e, "num", None) == 5:
            step = 1
        else:
            step = -1 if e.delta > 0 else 1
        self.canvas.yview_scroll(step, "units")


# --------------------------------------------------------------------------
# Visual preview (crafting grids, arrows, "X" marks) - slots are clickable
# --------------------------------------------------------------------------
SLOT = 56   # slot size in pixels
GAP = 4

ACTION_NOTES = {
    "remove": "Completely removed from the game (creative tabs, recipe viewers, loot, inventories, drops, villager trades...).",
    "remove_interactions": "Can no longer be used (right-click).",
    "remove_attacks": "Can no longer be used to attack entities.",
    "remove_effect": "Effect removed from every source.",
    "remove_potion": "Potions and tipped arrows with these effects are hidden.",
    "remove_enchantment": "Enchantment removed everywhere.",
}

_COUNT_RE = re.compile(r"^(.*?)(?:\s+[x\u00d7]\s*|\s*\*\s*)(\d+)$")
_KEY_CHARS = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
HINT = "Click a slot to type an item ID (clear it to remove, dashed + adds). Blue = tag, purple = regex."


def color_for(name):
    h = int(hashlib.md5(name.encode("utf-8")).hexdigest()[:6], 16)
    r, g, b = colorsys.hls_to_rgb((h % 360) / 360.0, 0.55, 0.55)
    return "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))


def vals(v):
    """str | list -> list of strings."""
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return [x for x in v if isinstance(x, str)]
    return []


def ing_options(v, default_kind="item"):
    """Normalise an ingredient into [(kind, id), ...]; kind is item/tag/regex/..."""
    out = []
    if isinstance(v, str):
        s = v.strip()
        if s.startswith("#"):
            out.append(("tag", s[1:]))
        elif s.startswith("tag:"):
            out.append(("tag", s[4:]))
        elif len(s) > 1 and s.startswith("/") and s.endswith("/"):
            out.append(("regex", s))
        elif s:
            out.append((default_kind, s))
    elif isinstance(v, dict):
        if "item" in v:
            out.append(("item", str(v["item"])))
        elif "tag" in v:
            out.append(("tag", str(v["tag"])))
        elif "id" in v:
            out.append(("item", str(v["id"])))
    elif isinstance(v, list):
        for x in v:
            out.extend(ing_options(x, default_kind))
    return out


def stack(v):
    """Result/output value -> (options, count)."""
    if isinstance(v, dict):
        ident = v.get("id", v.get("item"))
        count = v.get("count")
        return (ing_options(str(ident)) if ident is not None else []), (count if isinstance(count, int) else None)
    return ing_options(v), None


def bind_wheel(owner, canvas):
    def handler(e):
        try:
            w = owner.winfo_containing(e.x_root, e.y_root)
        except (KeyError, tk.TclError):
            return
        while w is not None and w is not owner:
            w = w.master
        if w is None:
            return
        if getattr(e, "num", None) == 4:
            step = -1
        elif getattr(e, "num", None) == 5:
            step = 1
        else:
            step = -1 if e.delta > 0 else 1
        canvas.yview_scroll(step, "units")
    for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        owner.bind_all(seq, handler, add="+")


class PreviewScenes:
    """Drawing + editing logic. Needs self.canvas, self.set_info() and the f_* fonts."""
    profile = None
    rule = None

    # hooks the real widget overrides
    def _begin_edit(self, tag):
        pass

    def _end_edit(self, commit):
        pass

    def _changed(self):
        cb = getattr(self, "on_change", None)
        if cb:
            cb(copy.deepcopy(self.rule))
        else:
            self.render(self.rule, self.profile)

    # ----- entry point -------------------------------------------------------
    def render(self, rule, profile):
        self._end_edit(False)
        self.rule = copy.deepcopy(rule)
        self.profile = profile
        self.canvas.delete("all")
        self._tipn = 0
        self._y = 10
        self._edits = {}
        self.set_info("")
        try:
            self._dispatch(self.rule, profile)
        except Exception as e:  # a preview bug must never break editing
            self._note(f"Couldn't draw a preview: {e}")
        bb = self.canvas.bbox("all")
        w, h = (bb[2], bb[3]) if bb else (0, 0)
        self.canvas.configure(scrollregion=(0, 0, w + 14, h + 14))

    def _dispatch(self, rule, profile):
        action = rule.get("action")
        canon = profile.canonical(action) if isinstance(action, str) else ""
        if canon == "add_recipe":
            rid = vals(rule.get("id"))
            self._title("Adds recipe  " + (rid[0] if rid else "(ID taken from the file name)"))
            rec = rule.get("recipe")
            if isinstance(rec, dict):
                self._recipe(rec)
            else:
                self._note("Fill in the 'recipe' field to see it here.")
        elif not canon and isinstance(rule.get("type"), str):
            self._title("Standalone recipe file")
            self._recipe(rule)
        elif not canon:
            self._note("Choose an action to see a preview.")
        elif profile.key == "recipes":
            self._recipes_action(canon, rule)
        else:
            self._remover_action(canon, rule)

    # ----- layout helpers ----------------------------------------------------
    def _text_block(self, text, font, fill="#222222", width=540):
        item = self.canvas.create_text(10, self._y, text=text, anchor="nw", width=width, font=font, fill=fill)
        bb = self.canvas.bbox(item)
        self._y += (bb[3] - bb[1] if bb else 14) + 8

    def _title(self, text):
        self._text_block(text, self.f_bold, "#111111")

    def _note(self, text):
        self._text_block(text, self.f_small, "#333333")

    def _row(self, elements):
        c = self.canvas
        widths, body = [], SLOT
        for e in elements:
            k = e[0]
            if k == "grid":
                cols, rows = e[2], e[3]
                widths.append(cols * SLOT + (cols - 1) * GAP)
                body = max(body, rows * SLOT + (rows - 1) * GAP)
            elif k == "slot":
                widths.append(SLOT)
            elif k == "arrow":
                widths.append(46)
            elif k == "plus":
                widths.append(24)
            else:
                widths.append(e[2])
        extra = 16 if any(e[0] == "slot" and e[4] for e in elements) else 0
        cy = self._y + body / 2
        x = 10
        for e, w in zip(elements, widths):
            k = e[0]
            if k == "grid":
                gh = e[3] * SLOT + (e[3] - 1) * GAP
                self._draw_grid(x, cy - gh / 2, e[1], e[2], e[3], e[4])
            elif k == "slot":
                self._draw_slot(x, cy - SLOT / 2, e[1], e[2], e[3], e[4], e[5])
            elif k == "arrow":
                c.create_line(x, cy, x + 38, cy, arrow="last", arrowshape=(12, 14, 6), width=5, fill="#4a4a4a")
            elif k == "plus":
                c.create_text(x + 12, cy, text="+", font=self.f_big, fill="#333333")
            else:
                c.create_text(x, cy, text=e[1], width=e[2], anchor="w", font=self.f_small,
                              fill="#222222", justify="left")
            x += w + 10
        self._y += body + extra + 14

    def _slot_flow(self, slots, per_row=6):
        for i in range(0, len(slots), per_row):
            self._row(slots[i:i + per_row])

    # ----- slot builders -------------------------------------------------------
    @staticmethod
    def _slot(opts, count=None, cross=False, caption=None, edit=None):
        return ("slot", opts, count, cross, caption, edit)

    @staticmethod
    def _ed(text, setter, add=False):
        return {"text": text, "set": setter, "add": add}

    @staticmethod
    def _opts(v, kind="item"):
        if kind == "mod":
            return [("mod", v)] if v else []
        if kind == "regex":
            return [("regex", v)] if v else []
        return ing_options(v, kind)

    @staticmethod
    def _opts_text(opts):
        return ", ".join(("#" + i if k == "tag" else i) for k, i in opts)

    def _list_slots(self, key, kind="item", cross=False, caption=None, add=True):
        """One editable slot per value of a str/list field, plus a dashed '+' slot."""
        values = vals(self.rule.get(key))
        out = []
        for i, v in enumerate(values):
            out.append(self._slot(self._opts(v, kind), None, cross, caption if i == 0 else None,
                                  self._ed(v, lambda t, i=i: self._set_value(key, i, t))))
        if add:
            out.append(self._slot([], None, False, None if values else caption,
                                  self._ed("", lambda t: self._set_value(key, None, t), add=True)))
        return out

    def _single_slot(self, key, kind="item", cross=False, caption=None):
        values = vals(self.rule.get(key))
        v = values[0] if values else ""
        return self._slot(self._opts(v, kind), None, cross, caption,
                          self._ed(v, lambda t: self._set_value(key, 0, t)))

    # ----- drawing primitives ------------------------------------------------
    def _draw_grid(self, x, y, cells, cols, rows, edits=None):
        for r in range(rows):
            for c in range(cols):
                i = r * cols + c
                opts = cells[i] if i < len(cells) else []
                ed = edits[i] if edits and i < len(edits) else None
                self._draw_slot(x + c * (SLOT + GAP), y + r * (SLOT + GAP), opts, None, False, None, ed)

    def _draw_slot(self, x, y, opts, count, cross, caption, edit=None):
        c = self.canvas
        kind = opts[0][0] if opts else None
        add = bool(edit and edit.get("add"))
        border = {"tag": "#2563eb", "regex": "#7c3aed", "mod": "#b45309"}.get(kind, "#373737")
        width = 3 if kind in ("tag", "regex", "mod") else 1
        self._tipn += 1
        tag = f"s{self._tipn}"
        tags = (tag,)
        tip = ""
        if opts:
            tip = ",  ".join(("#" if k == "tag" else "mod: " if k == "mod" else "") + i for k, i in opts)
            if isinstance(count, int) and count > 1:
                tip += f"   (x{count})"
        if edit:
            self._edits[tag] = {"x": x, "y": y, "text": edit["text"], "set": edit["set"], "add": add}
            c.tag_bind(tag, "<Button-1>", lambda e, t=tag: self._begin_edit(t))
        if tip or edit:
            def enter(e, t=tip, ed=bool(edit)):
                self.set_info((t + "     " if t else "") + ("(click to edit)" if ed else ""))
                if ed:
                    c.configure(cursor="hand2")

            def leave(e):
                self.set_info("")
                c.configure(cursor="")
            c.tag_bind(tag, "<Enter>", enter)
            c.tag_bind(tag, "<Leave>", leave)
        rect_kw = {"dash": (4, 3)} if add else {}
        c.create_rectangle(x, y, x + SLOT, y + SLOT, fill=("#a9a9a9" if add else "#8b8b8b"),
                           outline=border, width=width, tags=tags, **rect_kw)
        if add:
            c.create_text(x + SLOT / 2, y + SLOT / 2, text="+", font=self.f_big, fill="#555555", tags=tags)
        if opts:
            ident = opts[0][1]
            c.create_rectangle(x + 16, y + 5, x + SLOT - 16, y + 29,
                               fill=("#d4d4d8" if kind == "regex" else color_for(ident)),
                               outline="#222222", tags=tags)
            name = ident if kind in ("mod", "regex") else ident.split(":", 1)[-1]
            name = name.strip("/").replace("_", " ")
            if len(name) > 18:
                name = name[:17] + "\u2026"
            c.create_text(x + SLOT / 2, y + 32, text=name, width=SLOT - 4, anchor="n",
                          font=self.f_tiny, fill="#111111", justify="center", tags=tags)
            marker = {"tag": "#", "regex": ".*", "mod": "mod"}.get(kind)
            if marker:
                c.create_text(x + 4, y + 2, text=marker, anchor="nw", font=self.f_tiny, fill=border, tags=tags)
            if len(opts) > 1:
                c.create_text(x + SLOT - 3, y + 2, text=f"+{len(opts) - 1}", anchor="ne",
                              font=self.f_tiny, fill="#111111", tags=tags)
        if isinstance(count, int) and count > 1:
            c.create_text(x + SLOT - 2, y + SLOT - 2, text=str(count), anchor="se", font=self.f_count,
                          fill="#000000", tags=tags)
            c.create_text(x + SLOT - 3, y + SLOT - 3, text=str(count), anchor="se", font=self.f_count,
                          fill="#ffffff", tags=tags)
        if cross:
            c.create_line(x + 4, y + 4, x + SLOT - 4, y + SLOT - 4, width=4, fill="#cc2222", tags=tags)
            c.create_line(x + SLOT - 4, y + 4, x + 4, y + SLOT - 4, width=4, fill="#cc2222", tags=tags)
        if caption:
            c.create_text(x + SLOT / 2, y + SLOT + 2, text=caption, anchor="n", font=self.f_small, fill="#333333")

    # ----- editing: top-level fields ------------------------------------------
    def _set_value(self, key, idx, text):
        """Set/append/remove one entry of a rule field (idx None = append)."""
        text = text.strip()
        if self.profile.field_kind(key) == "str":
            if text:
                self.rule[key] = text
            else:
                self.rule.pop(key, None)
        else:
            items = vals(self.rule.get(key))
            if idx is None or idx >= len(items):
                if text:
                    items.append(text)
            elif text:
                items[idx] = text
            else:
                del items[idx]
            if items:
                self.rule[key] = items
            else:
                self.rule.pop(key, None)
        self._changed()

    # ----- editing: recipe objects --------------------------------------------
    @staticmethod
    def _ing_value(text, like=None):
        """'a, #b' -> ingredient JSON, matching the style (dict vs plain string) already in use."""
        parts = [p.strip() for p in text.split(",") if p.strip()]
        if not parts:
            return None
        as_str = isinstance(like, str) or (isinstance(like, list) and like and all(isinstance(x, str) for x in like))

        def one(p):
            tag = p[1:] if p.startswith("#") else (p[4:] if p.startswith("tag:") else None)
            if as_str:
                return ("#" + tag) if tag is not None else p
            return {"tag": tag} if tag is not None else {"item": p}
        out = [one(p) for p in parts]
        return out[0] if len(out) == 1 else out

    def _set_ing(self, rec, key, text):
        v = self._ing_value(text, rec.get(key))
        if v is None:
            rec.pop(key, None)
        else:
            rec[key] = v
        self._changed()

    def _set_ing_item(self, rec, key, idx, text):
        lst = rec.get(key)
        lst = list(lst) if isinstance(lst, list) else ([] if lst is None else [lst])
        v = self._ing_value(text, lst[0] if lst else None)
        if idx is None or idx >= len(lst):
            if v is not None:
                lst.append(v)
        elif v is None:
            del lst[idx]
        else:
            lst[idx] = v
        rec[key] = lst
        self._changed()

    def _set_result(self, rec, key, text):
        text = text.strip()
        ident, count = text, None
        m = _COUNT_RE.match(text)
        if m:
            ident, count = m.group(1).strip(), int(m.group(2))
        cur = rec.get(key)
        if not ident:
            rec.pop(key, None)
        elif isinstance(cur, dict):
            new = dict(cur)
            new["item" if ("item" in cur and "id" not in cur) else "id"] = ident
            if count is not None:
                new["count"] = count
            rec[key] = new
        elif isinstance(cur, str) and count is None:
            rec[key] = ident
        elif isinstance(cur, str) and "count" in rec:
            rec[key] = ident
            rec["count"] = count
        else:
            new = {"id": ident}
            if count is not None:
                new["count"] = count
            rec[key] = new
        self._changed()

    def _set_shaped_cell(self, rec, r, c, text):
        pattern = rec.get("pattern")
        pattern = [p for p in pattern if isinstance(p, str)] if isinstance(pattern, list) else []
        key = rec.get("key") if isinstance(rec.get("key"), dict) else {}
        rows = max(len(pattern), r + 1)
        cols = max([len(p) for p in pattern] + [c + 1])
        grid = [list(p.ljust(cols)) for p in pattern] + [[" "] * cols for _ in range(rows - len(pattern))]
        v = self._ing_value(text, next(iter(key.values()), None))
        if v is None:
            grid[r][c] = " "
        else:
            ch = next((k for k, val in key.items() if val == v and len(k) == 1), None)
            if ch is None:
                ch = next((x for x in _KEY_CHARS if x not in key), "?")
                key[ch] = v
            grid[r][c] = ch
        lines = ["".join(row) for row in grid]
        while lines and not lines[-1].strip():
            lines.pop()
        width = max([len(x.rstrip()) for x in lines] + [0])
        pattern = [x[:width].ljust(width) for x in lines]
        used = set("".join(pattern))
        key = {k: val for k, val in key.items() if k in used}
        rec["pattern"] = pattern
        rec["key"] = key
        self._changed()

    # ----- recipe scenes (editable) --------------------------------------------
    def _ing_slot(self, rec, key, caption=None):
        opts = ing_options(rec.get(key))
        return self._slot(opts, None, False, caption,
                          self._ed(self._opts_text(opts), lambda t: self._set_ing(rec, key, t)))

    def _res_slot(self, rec, key, caption="Result"):
        opts, count = stack(rec.get(key))
        if count is None and key == "result" and isinstance(rec.get("count"), int):
            count = rec["count"]
        text = (opts[0][1] if opts else "") + (f" x{count}" if isinstance(count, int) and count > 1 else "")
        return self._slot(opts, count, False, caption, self._ed(text, lambda t: self._set_result(rec, key, t)))

    def _recipe(self, rec):
        typ = str(rec.get("type", ""))
        t = typ.split(":", 1)[-1]
        self._title(typ or "Recipe (no type)")
        rkey = "output" if (t == "brewing" or ("output" in rec and "result" not in rec)) else "result"
        res = self._res_slot(rec, rkey)
        ing = self._ing_slot

        if t == "crafting_shaped":
            pattern = rec.get("pattern")
            pattern = [r for r in pattern if isinstance(r, str)] if isinstance(pattern, list) else []
            key = rec.get("key") if isinstance(rec.get("key"), dict) else {}
            rows = max([3, len(pattern)])
            cols = max([3] + [len(r) for r in pattern])
            cells, edits = [], []
            for r in range(rows):
                for c in range(cols):
                    ch = pattern[r][c] if r < len(pattern) and c < len(pattern[r]) else " "
                    opts = ing_options(key.get(ch)) if ch != " " and ch in key else []
                    cells.append(opts)
                    edits.append(self._ed(self._opts_text(opts),
                                          lambda tx, r=r, c=c: self._set_shaped_cell(rec, r, c, tx)))
            self._row([("grid", cells, cols, rows, edits), ("arrow",), res])
            missing = sorted({ch for r in pattern for ch in r if ch != " " and ch not in key})
            if missing:
                self._note("Pattern uses " + ", ".join(repr(m) for m in missing) + " but 'key' has no entry for it.")
            self._note("Shaped: ingredients must be placed in this arrangement.")
        elif t == "crafting_shapeless":
            items = rec.get("ingredients", [])
            items = items if isinstance(items, list) else [items]
            cols = 3
            rows = max(3, -(-(len(items) + 1) // 3))
            cells = [ing_options(i) for i in items]
            edits = [self._ed(self._opts_text(cells[i]) if i < len(cells) else "",
                              lambda tx, i=i: self._set_ing_item(rec, "ingredients", i, tx))
                     for i in range(rows * cols)]
            self._row([("grid", cells, cols, rows, edits), ("arrow",), res])
            self._note("Shapeless: ingredients can go anywhere in the grid.")
        elif t == "crafting_transmute":
            mc = rec.get("material_count")
            mat = ing(rec, "material", "Material")
            mat = mat[:2] + (mc if isinstance(mc, int) else None,) + mat[3:]
            self._row([ing(rec, "input", "Item"), ("plus",), mat, ("arrow",), res])
            self._note("Upgrades the item while keeping its enchantments, durability and name.")
        elif t in ("smelting", "blasting", "smoking", "campfire_cooking"):
            self._row([ing(rec, "ingredient", "Input"), ("arrow",), res])
            bits = []
            if isinstance(rec.get("cookingtime"), int):
                bits.append(f"cooking time {rec['cookingtime']} ticks ({rec['cookingtime'] / 20:g} s)")
            if "experience" in rec:
                bits.append(f"{rec['experience']} xp")
            if bits:
                self._note(", ".join(bits))
        elif t == "stonecutting":
            self._row([ing(rec, "ingredient", "Input"), ("arrow",), res])
        elif t in ("smithing_transform", "smithing_trim"):
            tail = [("arrow",), res] if t == "smithing_transform" else [("arrow",), ("text", "Trimmed base item", 100)]
            self._row([ing(rec, "template", "Template"), ("plus",), ing(rec, "base", "Base"), ("plus",),
                       ing(rec, "addition", "Addition")] + tail)
        elif t == "brewing":
            self._row([ing(rec, "input", "Input"), ("plus",), ing(rec, "reagent", "Reagent"), ("arrow",), res])
        else:
            row = [ing(rec, k, k) for k in ("template", "base", "input", "ingredient", "addition", "material")
                   if k in rec]
            if isinstance(rec.get("ingredients"), list):
                row += [self._slot(ing_options(i), None, False, None,
                                   self._ed(self._opts_text(ing_options(i)),
                                            lambda tx, n=n: self._set_ing_item(rec, "ingredients", n, tx)))
                        for n, i in enumerate(rec["ingredients"][:6])]
            if row and (rec.get(rkey) is not None):
                self._row(row + [("arrow",), res])
            else:
                self._note("No visual layout for this recipe type. Fields: " + ", ".join(rec.keys()))
        extra = [f"{k}: {rec[k]}" for k in ("group", "category") if isinstance(rec.get(k), str)]
        if extra:
            self._note("   ".join(extra))

    # ----- Reliable Recipes rule scenes ---------------------------------------
    @staticmethod
    def _filters_text(rule, keys):
        parts = [f"{k}: {', '.join(vals(rule[k]))}" for k in keys if k in rule and vals(rule[k])]
        return "  |  ".join(parts) or "ALL recipes"

    def _recipes_action(self, canon, rule):
        flt = self._filters_text(rule, ("output", "id", "mod", "type", "input"))
        L = self._list_slots
        if canon == "replace_input":
            self._title("Replace ingredient")
            self._row(L("target", "item", caption="Find") + [("arrow",)] + L("replacement", "item", caption="Replace with"))
            self._note("In recipes matching:  " + flt)
        elif canon == "replace_output":
            self._title("Replace recipe output")
            self._row([("text", "Matching recipes\nproduce", 110), ("arrow",),
                       self._single_slot("replacement", caption="Now produces")])
            self._note("Recipes matching:  " + flt)
        elif canon == "prevent_repair":
            self._title("Prevent repair")
            self._slot_flow(L("target", cross=True, caption="Item(s)"))
            self._note("These items can't be repaired by any standard method.")
        elif canon == "set_repair_material":
            self._title("Change repair material")
            self._row(L("target", caption="Item") + [("text", "is repaired\nwith", 90)] + L("material", caption="Material"))
        elif canon == "remove_recipe":
            self._title("Remove recipes")
            self._slot_flow(L("output", cross=True, caption="Output"))
            self._note("Removed recipes:  " + flt)
        elif canon == "remove_all_tags":
            self._title("Remove from every tag")
            self._slot_flow(L("id", caption="Item(s)"))
        elif canon == "remove_from_tag":
            self._title("Remove from one tag")
            self._row(L("tag", "tag", caption="Tag") + [("text", "no longer contains", 110)])
            self._slot_flow(L("id", cross=True, caption="Item(s)"))
        elif canon == "clear_tag":
            self._title("Empty tags")
            self._slot_flow(L("tags", "tag", cross=True, caption="Tag(s)"))
            self._note("Every item/block is removed from these tags.")
        else:
            self._note("No visual preview for this action.")

    # ----- Reliable Remover rule scenes ----------------------------------------
    def _remover_action(self, canon, rule):
        self._title(canon)
        primary = {"remove_effect": "effects", "remove_potion": "effects",
                   "remove_enchantment": "enchantments"}.get(canon, "items")
        kinds = {"items": "item", "fluids": "fluid", "mod": "mod", "effects": "effect", "enchantments": "enchant"}
        slots = []
        for key in ("items", "fluids", "mod", "effects", "enchantments"):
            if key == primary or vals(rule.get(key)):
                slots += self._list_slots(key, kinds[key], cross=True,
                                          caption=("Remove" if key == primary else None), add=False)
        if vals(rule.get("pattern")):
            slots.append(self._single_slot("pattern", "regex", cross=True))
        slots.append(self._slot([], None, False, None if slots else "Remove",
                                self._ed("", lambda t, k=primary: self._set_value(k, None, t), add=True)))
        self._slot_flow(slots)
        if canon == "remove" or isinstance(rule.get("replace_with"), str):
            self._row([("text", "replace with", 90), self._single_slot("replace_with", caption="Optional")])
        if ACTION_NOTES.get(canon):
            self._note(ACTION_NOTES[canon])
        if vals(rule.get("entities")):
            self._note("Entities:  " + ", ".join(vals(rule["entities"])))
        if vals(rule.get("dimensions")):
            self._note("Only in:  " + ", ".join(vals(rule["dimensions"])))
        if isinstance(rule.get("not"), dict):
            self._note("Except:  " + json.dumps(rule["not"], ensure_ascii=False))


class Preview(PreviewScenes, ttk.Frame):
    def __init__(self, master):
        ttk.Frame.__init__(self, master)
        self.on_change = None
        self._entry = None
        self._edits = {}
        fam = tkfont.nametofont("TkDefaultFont").actual("family")
        self.f_tiny = tkfont.Font(family=fam, size=7)
        self.f_small = tkfont.Font(family=fam, size=9)
        self.f_bold = tkfont.Font(family=fam, size=10, weight="bold")
        self.f_big = tkfont.Font(family=fam, size=16, weight="bold")
        self.f_count = tkfont.Font(family=fam, size=9, weight="bold")
        self.canvas = tk.Canvas(self, background="#c6c6c6", highlightthickness=0)
        ysb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        xsb = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew")
        self.info_var = tk.StringVar()
        ttk.Label(self, textvariable=self.info_var, foreground="#444444").grid(row=2, column=0, columnspan=2, sticky="w")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        bind_wheel(self, self.canvas)

    def set_info(self, text):
        self.info_var.set(text if text else HINT)

    # inline editing: a text entry appears over the clicked slot
    def _begin_edit(self, tag):
        if self._entry:                      # a click while editing just commits
            self._end_edit(True)
            return
        info = self._edits.get(tag)
        if not info:
            return
        ent = ttk.Entry(self.canvas, font=self.f_small)
        ent.insert(0, info["text"])
        win = self.canvas.create_window(info["x"], info["y"] + SLOT / 2 - 11, window=ent, anchor="nw", width=280)
        self._entry = (ent, win, info)
        ent.focus_set()
        ent.select_range(0, "end")
        ent.bind("<Return>", lambda e: self._end_edit(True))
        ent.bind("<Escape>", lambda e: self._end_edit(False))
        ent.bind("<FocusOut>", lambda e: self._end_edit(True))

    def _end_edit(self, commit):
        if not self._entry:
            return
        ent, win, info = self._entry
        self._entry = None
        text = ent.get()
        try:
            self.canvas.delete(win)
            ent.destroy()
        except tk.TclError:
            pass
        if commit and text.strip() != info["text"].strip():
            info["set"](text)


class AutoText(ttk.Frame):
    """A text box that grows with its content up to max_lines, then scrolls."""

    def __init__(self, master, min_lines=2, max_lines=4, font=None):
        super().__init__(master)
        self.min_lines, self.max_lines = min_lines, max_lines
        self._sb_shown = False
        self.text = tk.Text(self, height=min_lines, width=36, wrap="none", font=font, undo=True)
        self.sb = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=self.sb.set)
        self.text.pack(side="left", fill="both", expand=True)
        for seq in ("<KeyRelease>", "<<Paste>>", "<<Cut>>", "<<Undo>>", "<<Redo>>"):
            self.text.bind(seq, lambda e: self.after_idle(self.fit), add="+")
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.text.bind(seq, self._wheel)

    def _lines(self):
        return int(self.text.index("end-1c").split(".")[0])

    def fit(self):
        lines = self._lines()
        h = max(self.min_lines, min(self.max_lines, lines))
        if int(self.text.cget("height")) != h:
            self.text.configure(height=h)
        need = lines > self.max_lines
        if need and not self._sb_shown:
            self.sb.pack(side="right", fill="y")
            self._sb_shown = True
        elif not need and self._sb_shown:
            self.sb.pack_forget()
            self._sb_shown = False

    def _wheel(self, e):
        if self._lines() <= self.max_lines:
            return None                       # let the page scroll instead
        if getattr(e, "num", None) == 4:
            step = -1
        elif getattr(e, "num", None) == 5:
            step = 1
        else:
            step = -1 if e.delta > 0 else 1
        self.text.yview_scroll(step, "units")
        return "break"

    # Text-like API used by RuleEditor
    def get(self, a, b=None):
        return self.text.get(a, b)

    def delete(self, a, b=None):
        self.text.delete(a, b)
        self.fit()

    def insert(self, idx, chars):
        self.text.insert(idx, chars)
        self.fit()


# --------------------------------------------------------------------------
# Rule editor (form + raw JSON)
# --------------------------------------------------------------------------
class RuleEditor(ttk.Frame):
    def __init__(self, master, profile):
        super().__init__(master)
        self.p = profile
        self.widgets = {}          # key -> (Field, widget)
        self.extras = None
        self.built_action = None
        self._snapshot = None
        self._shown = "form"
        self._raw_loaded = ""
        self._after = None
        self.on_change = None      # called after the preview edits the rule
        mono = tkfont.nametofont("TkFixedFont")
        self.mono = mono
        self.small = tkfont.nametofont("TkDefaultFont").copy()
        base = self.small.cget("size")
        self.small.configure(size=base - 1 if base > 0 else base + 1)

        head = ttk.Frame(self)
        head.pack(fill="x", pady=(0, 4))
        ttk.Label(head, text="Action:").pack(side="left")
        self.action_var = tk.StringVar()
        self.action_box = ttk.Combobox(head, textvariable=self.action_var,
                                       values=profile.action_names, width=26)
        self.action_box.pack(side="left", padx=6)
        self.action_box.bind("<<ComboboxSelected>>", self._action_changed)
        self.action_box.bind("<Return>", self._action_changed)

        self.msg_var = tk.StringVar()
        self.msg = ttk.Label(self, textvariable=self.msg_var, wraplength=520, justify="left")
        self.msg.pack(fill="x", pady=(0, 4))

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True)
        self.scroll = ScrollFrame(self.nb)
        self.form = self.scroll.inner
        self.nb.add(self.scroll, text="Form")
        raw_frame = ttk.Frame(self.nb)
        self.raw = tk.Text(raw_frame, wrap="none", font=mono, undo=True, height=12, width=50)
        rsb = ttk.Scrollbar(raw_frame, orient="vertical", command=self.raw.yview)
        self.raw.configure(yscrollcommand=rsb.set)
        self.raw.pack(side="left", fill="both", expand=True)
        rsb.pack(side="right", fill="y")
        self.nb.add(raw_frame, text="Raw JSON")
        self.preview = Preview(self.nb)
        self.preview.on_change = self._preview_edited
        self.nb.add(self.preview, text="Preview")
        self._view = "form"
        self.nb.bind("<<NotebookTabChanged>>", self._tab_changed)

        self.winfo_toplevel().bind("<KeyRelease>", self._on_key, add="+")
        self._build("")

    # ----- building / loading -------------------------------------------
    def _build(self, action):
        for w in self.form.winfo_children():
            w.destroy()
        self.widgets = {}
        r = 0
        n_primary = self.p.primary.get(self.p.canonical(action))
        for i, f in enumerate(self.p.fields_for(action)):
            if n_primary is not None and i == n_primary:
                ttk.Label(self.form, text="Not used by this action (blank = ignored)",
                          foreground="#999999", font=self.small).grid(row=r, column=0, columnspan=2,
                                                                       sticky="w", pady=(16, 0))
                r += 1
            ttk.Label(self.form, text=f.label).grid(row=r, column=0, sticky="nw", padx=(2, 10), pady=(8, 0))
            if f.kind == "str":
                var = tk.StringVar()
                w = ttk.Entry(self.form, textvariable=var, font=self.mono)
                w.var = var
            elif f.kind == "list":
                w = AutoText(self.form, min_lines=2, max_lines=4, font=self.mono)
            else:
                w = AutoText(self.form, min_lines=8, max_lines=24, font=self.mono)
            w.grid(row=r, column=1, sticky="ew", pady=(8, 0))
            if f.hint:
                ttk.Label(self.form, text=f.hint, foreground="#777777", font=self.small).grid(
                    row=r + 1, column=1, sticky="w")
            self.widgets[f.key] = (f, w)
            r += 2
        ttk.Label(self.form, text="Other fields\n(JSON)").grid(row=r, column=0, sticky="nw", padx=(2, 10), pady=(12, 0))
        self.extras = AutoText(self.form, min_lines=3, max_lines=10, font=self.mono)
        self.extras.grid(row=r, column=1, sticky="ew", pady=(12, 0))
        ttk.Label(self.form, text="Keys the form doesn't cover are kept here, e.g. {\"some_key\": 1}",
                  foreground="#777777", font=self.small).grid(row=r + 1, column=1, sticky="w")
        self.form.columnconfigure(1, weight=1)
        self.built_action = action

    @staticmethod
    def _read(field, w):
        if field.kind == "str":
            return w.var.get()
        return w.get("1.0", "end-1c")

    @staticmethod
    def _write(field, w, text):
        if field.kind == "str":
            w.var.set(text)
        else:
            w.delete("1.0", "end")
            w.insert("1.0", text)

    @staticmethod
    def _to_text(field, v):
        if field.kind == "str":
            return v if isinstance(v, str) else None
        if field.kind == "list":
            if isinstance(v, str):
                return v
            if isinstance(v, list) and all(isinstance(x, str) for x in v):
                return "\n".join(v)
            return None
        return json.dumps(v, indent=2, ensure_ascii=False)

    def _load_form(self, rule):
        action = rule.get("action") if isinstance(rule.get("action"), str) else ""
        self.action_var.set(action)
        self._build(action)
        extras = {}
        for k, v in rule.items():
            if k == "action" and isinstance(v, str):
                continue
            entry = self.widgets.get(k)
            if entry is None:
                extras[k] = v
                continue
            field, w = entry
            text = self._to_text(field, v)
            if text is None:
                extras[k] = v
            else:
                self._write(field, w, text)
        if extras:
            self.extras.insert("1.0", json.dumps(extras, indent=2, ensure_ascii=False))

    def load(self, rule):
        rule = rule or {}
        self._load_form(rule)
        self._shown = "form"
        self._view = "form"
        self.nb.select(self.scroll)
        self._set_raw(rule)
        self._snapshot = self.state()
        self._validate_now()

    def _set_raw(self, rule):
        text = json.dumps(rule, indent=2, ensure_ascii=False)
        self.raw.delete("1.0", "end")
        self.raw.insert("1.0", text)
        self._raw_loaded = text

    # ----- reading back ----------------------------------------------------
    def _parse_field(self, f, raw):
        if f.kind == "str":
            return raw.strip() or None
        if f.kind == "list":
            lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
            if not lines:
                return None
            if self.p.collapse_single and len(lines) == 1:
                return lines[0]
            return lines
        s = raw.strip()
        if not s:
            return None
        try:
            return json.loads(s)
        except json.JSONDecodeError as e:
            raise ValueError(f"'{f.label}' is not valid JSON: {e}")

    def _form_rule(self):
        rule = {}
        action = self.action_var.get().strip()
        if action:
            rule["action"] = action
        for key, (f, w) in self.widgets.items():
            val = self._parse_field(f, self._read(f, w))
            if val is not None:
                rule[key] = val
        txt = self.extras.get("1.0", "end-1c").strip()
        if txt:
            try:
                extra = json.loads(txt)
            except json.JSONDecodeError as e:
                raise ValueError(f"'Other fields' is not valid JSON: {e}")
            if not isinstance(extra, dict):
                raise ValueError("'Other fields' must be a JSON object like { ... }")
            for k, v in extra.items():
                if k in rule:
                    raise ValueError(f"'{k}' is set both in the form and in 'Other fields'.")
                rule[k] = v
        return rule

    def _raw_rule(self):
        try:
            data = json.loads(self.raw.get("1.0", "end-1c"))
        except json.JSONDecodeError as e:
            raise ValueError(f"Raw JSON is invalid: {e}")
        if not isinstance(data, dict):
            raise ValueError("Raw JSON must be a single object { ... }")
        return data

    def get_rule(self):
        return self._raw_rule() if self._shown == "raw" else self._form_rule()

    def state(self):
        if self._shown == "raw":
            return ("raw", self.raw.get("1.0", "end-1c"))
        return ("form", self.action_var.get(),
                tuple(self._read(f, w) for f, w in self.widgets.values()),
                self.extras.get("1.0", "end-1c"))

    def is_changed(self):
        return self.state() != self._snapshot

    def mark_saved(self):
        self._snapshot = self.state()
        self._validate_now()

    # ----- events -----------------------------------------------------------
    def _action_changed(self, _=None):
        new = self.action_var.get().strip()
        if self._shown == "raw":  # keep the raw text in step with the dropdown
            try:
                rule = self._raw_rule()
            except ValueError:
                return
            if new:
                rule["action"] = new
            else:
                rule.pop("action", None)
            self.raw.delete("1.0", "end")
            self.raw.insert("1.0", json.dumps(rule, indent=2, ensure_ascii=False))
            self._validate_now()
            self._refresh_preview()
            return
        if new == self.built_action:
            return
        try:
            rule = self._form_rule()
        except ValueError as e:
            messagebox.showerror("Fix this first", str(e), parent=self)
            self.action_var.set(self.built_action)
            return
        if new:
            rule["action"] = new
        else:
            rule.pop("action", None)
        self._load_form(rule)
        for f, w in self.widgets.values():  # drop in templates for empty JSON fields
            if f.template and not self._read(f, w).strip():
                self._write(f, w, f.template)
        self._validate_now()
        self._refresh_preview()

    VIEWS = ("form", "raw", "preview")

    def _select_view(self, name):
        self.nb.select(self.nb.tabs()[self.VIEWS.index(name)])

    def _refresh_preview(self):
        if self._view == "preview":
            try:
                self.preview.render(self.get_rule(), self.p)
            except ValueError:
                pass

    def _tab_changed(self, _=None):
        now = self.VIEWS[self.nb.index(self.nb.select())]
        if now == self._view:
            return
        prev = self._view
        if now == "preview":
            try:
                rule = self.get_rule()
            except ValueError as e:
                messagebox.showerror("Can't preview yet", str(e), parent=self)
                self._select_view(prev)
                return
            self.preview.render(rule, self.p)
        elif now == "raw":
            if self._shown != "raw":
                try:
                    rule = self._form_rule()
                except ValueError as e:
                    messagebox.showerror("Can't switch yet", str(e), parent=self)
                    self._select_view(prev)
                    return
                self._set_raw(rule)
                self._shown = "raw"
        else:
            if self._shown != "form":
                if self.raw.get("1.0", "end-1c") != self._raw_loaded:
                    try:
                        rule = self._raw_rule()
                    except ValueError as e:
                        messagebox.showerror("Can't switch yet", str(e), parent=self)
                        self._select_view(prev)
                        return
                    self._load_form(rule)
                self._shown = "form"
        self._view = now
        self._validate_now()

    def _preview_edited(self, rule):
        """The preview changed the rule: push it into the form/raw views and re-draw."""
        self._load_form(rule)
        self._set_raw(rule)
        self._shown = "form"
        try:
            rule = self._form_rule()
        except ValueError:
            pass
        self.preview.render(rule, self.p)
        self._validate_now()
        if self.on_change:
            self.on_change()

    def _on_key(self, e):
        if not str(e.widget).startswith(str(self)):
            return
        if self._after:
            self.after_cancel(self._after)
        self._after = self.after(350, self._validate_now)

    def _validate_now(self):
        self._after = None
        if not self.winfo_exists():
            return
        try:
            msgs = self.p.validate(self.get_rule())
            color = "#b45309"
        except ValueError as e:
            msgs, color = [str(e)], "#b91c1c"
        self.msg.configure(foreground=color)
        self.msg_var.set("\n".join(msgs))


# --------------------------------------------------------------------------
# One tab = one mod
# --------------------------------------------------------------------------
class TabApp(ttk.Frame):
    def __init__(self, master, profile, settings, on_loaded):
        super().__init__(master, padding=8)
        self.p = profile
        self.settings = settings
        self.on_loaded = on_loaded
        self.files = {}
        self.folder = ""
        self.current = None
        self.iid_map = {}
        self.rule_iid = {}
        self.file_iid = {}
        self.open_state = {}
        self.backed_up = set()

        # --- folder bar
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Label(bar, text="Folder:").pack(side="left")
        self.folder_var = tk.StringVar(value=settings.get("folders", {}).get(profile.key, ""))
        ent = ttk.Entry(bar, textvariable=self.folder_var)
        ent.pack(side="left", fill="x", expand=True, padx=6)
        ent.bind("<Return>", lambda e: self.load_folder())
        ttk.Button(bar, text="Browse\u2026", command=self.browse).pack(side="left")
        ttk.Button(bar, text="Load", command=self.load_folder).pack(side="left", padx=(6, 0))
        ttk.Label(self, text=f"Pick your Minecraft instance folder, its config folder, or the "
                             f"{profile.folder_name} folder itself.",
                  foreground="#777777", font=self._small_font()).pack(anchor="w", pady=(2, 6))

        # --- main panes
        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True)

        left = ttk.Frame(panes)
        panes.add(left, weight=3)
        srow = ttk.Frame(left)
        srow.pack(fill="x", pady=(0, 4))
        ttk.Label(srow, text="Search:").pack(side="left")
        self.search_var = tk.StringVar()
        se = ttk.Entry(srow, textvariable=self.search_var)
        se.pack(side="left", fill="x", expand=True, padx=6)
        self.search_var.trace_add("write", lambda *a: self._refresh_keep())

        tree_frame = ttk.Frame(left)
        tree_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_frame, columns=("action", "summary"), selectmode="browse")
        self.tree.heading("#0", text="File / rule")
        self.tree.heading("action", text="Action")
        self.tree.heading("summary", text="Details")
        self.tree.column("#0", width=190, stretch=False)
        self.tree.column("action", width=120, stretch=False)
        self.tree.column("summary", width=260)
        tsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tsb.pack(side="right", fill="y")
        bold = tkfont.nametofont("TkDefaultFont").copy()
        bold.configure(weight="bold")
        self._bold = bold
        self.tree.tag_configure("file", font=bold)
        self.tree.tag_configure("bad", foreground="#b91c1c")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

        btns = ttk.Frame(left)
        btns.pack(fill="x", pady=(6, 0))
        for text, cmd in (("New file", self.new_file), ("New rule", self.new_rule),
                          ("Duplicate", self.duplicate_rule), ("Delete", self.delete_rule),
                          ("\u25B2", lambda: self.move_rule(-1)), ("\u25BC", lambda: self.move_rule(1))):
            ttk.Button(btns, text=text, command=cmd,
                       width=3 if len(text) == 1 else None).pack(side="left", padx=(0, 4))

        right = ttk.Frame(panes, padding=(10, 0, 0, 0))
        panes.add(right, weight=4)
        self.placeholder_var = tk.StringVar(value="Load a folder, then select a rule to edit it.")
        self.placeholder = ttk.Label(right, textvariable=self.placeholder_var, foreground="#777777",
                                     wraplength=420, justify="center")
        self.editor_box = ttk.Frame(right)
        self.editor = RuleEditor(self.editor_box, profile)
        self.editor.pack(fill="both", expand=True)
        self.editor.on_change = self._preview_commit
        ttk.Button(self.editor_box, text="Apply to rule", command=self.apply).pack(anchor="e", pady=(6, 0))
        self._set_editor_visible(False)

        # --- bottom bar
        bottom = ttk.Frame(self)
        bottom.pack(fill="x", pady=(8, 0))
        ttk.Button(bottom, text="Save all  (Ctrl+S)", command=self.save_all).pack(side="left")
        self.backup_var = tk.BooleanVar(value=settings.get("backup", True))
        ttk.Checkbutton(bottom, text="Keep .bak backup of the original on first save",
                        variable=self.backup_var).pack(side="left", padx=12)
        self.status_var = tk.StringVar()
        ttk.Label(bottom, textvariable=self.status_var, foreground="#555555").pack(side="right")

    # ----- misc helpers -------------------------------------------------
    def _small_font(self):
        self._small = tkfont.nametofont("TkDefaultFont").copy()
        base = self._small.cget("size")
        self._small.configure(size=base - 1 if base > 0 else base + 1)
        return self._small

    def status(self, text):
        self.status_var.set(text)

    def _set_editor_visible(self, visible):
        if visible:
            self.placeholder.pack_forget()
            self.editor_box.pack(fill="both", expand=True)
        else:
            self.editor_box.pack_forget()
            self.placeholder.pack(expand=True)

    def any_dirty(self):
        return any(st.dirty for st in self.files.values())

    def _rel(self, path):
        return os.path.relpath(path, self.folder) if self.folder else path

    def _selected(self):
        sel = self.tree.selection()
        return self.iid_map.get(sel[0]) if sel else None

    def _selected_rule(self):
        s = self._selected()
        if s and s[1] is not None:
            return s
        return None

    # ----- loading ---------------------------------------------------------
    def browse(self):
        start = self.folder_var.get().strip() or os.path.expanduser("~")
        d = filedialog.askdirectory(initialdir=start if os.path.isdir(start) else os.path.expanduser("~"),
                                    title="Select instance / config / " + self.p.folder_name + " folder",
                                    parent=self)
        if d:
            self.folder_var.set(d)
            self.load_folder()

    def _confirm_discard(self):
        if not self._commit_current():
            return False
        if self.any_dirty():
            return messagebox.askyesno("Unsaved changes", "Discard your unsaved changes and reload?", parent=self)
        return True

    def load_folder(self, silent=False):
        raw = self.folder_var.get().strip()
        folder = resolve_folder(raw, self.p.folder_name)
        if not folder:
            if not silent:
                messagebox.showinfo("Folder", "Choose a folder first.", parent=self)
            return
        if not silent and not self._confirm_discard():
            return
        self.folder = folder
        self.folder_var.set(folder)
        self.files = {}
        self.current = None
        self.backed_up = set()
        self._set_editor_visible(False)
        self.placeholder_var.set("Select a rule to edit it.")
        if os.path.isdir(folder):
            for root, dirs, names in os.walk(folder):
                dirs.sort()
                for n in sorted(names):
                    if n.lower().endswith(".json"):
                        path = os.path.join(root, n)
                        self.files[path] = self._read_file(path)
            total = sum(len(s.rules) for s in self.files.values())
            self.status(f"Loaded {len(self.files)} file(s), {total} rule(s)")
        else:
            self.status("Folder doesn't exist yet - it will be created when you save a new file")
            self.placeholder_var.set("This folder doesn't exist yet.\nUse \"New file\" to create your first rule file.")
        self.settings.setdefault("folders", {})[self.p.key] = folder
        self.refresh()
        self.on_loaded(self, folder)

    @staticmethod
    def _read_file(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as e:
            return FileState(path, [], False, error=f"Couldn't read this file: {e}")
        if isinstance(data, dict):
            return FileState(path, [data], True)
        if isinstance(data, list) and all(isinstance(x, dict) for x in data):
            return FileState(path, data, False)
        return FileState(path, [], False,
                         error="Unsupported structure (expected an object or a list of objects). Not editable here.")

    # ----- tree --------------------------------------------------------------
    def _file_label(self, st):
        return self._rel(st.path) + (" *" if st.dirty else "") + ("  (!)" if st.error else "")

    def refresh(self, select=None):
        t = self.tree
        for iid, (path, idx) in self.iid_map.items():
            if idx is None and t.exists(iid):
                self.open_state[path] = str(t.item(iid, "open")).lower() in ("1", "true", "yes")
        t.delete(*t.get_children())
        self.iid_map, self.rule_iid, self.file_iid = {}, {}, {}
        needle = self.search_var.get().strip().lower()
        n = 0
        for path in sorted(self.files, key=lambda p: p.lower()):
            st = self.files[path]
            rel = self._rel(path)
            file_match = needle in rel.lower()
            matches = []
            for i, rule in enumerate(st.rules):
                if (not needle or file_match
                        or needle in json.dumps(rule, ensure_ascii=False).lower()):
                    matches.append(i)
            if needle and not matches and not file_match:
                continue
            fid = f"f{n}"
            n += 1
            t.insert("", "end", iid=fid, text=self._file_label(st),
                     values=("", f"{len(st.rules)} rule(s)" if not st.error else "unreadable"),
                     open=self.open_state.get(path, True) or bool(needle),
                     tags=("file", "bad") if st.error else ("file",))
            self.iid_map[fid] = (path, None)
            self.file_iid[path] = fid
            for i in matches:
                rid = f"r{n}"
                n += 1
                rule = st.rules[i]
                act = rule.get("action") if isinstance(rule.get("action"), str) else "(no action)"
                t.insert(fid, "end", iid=rid, text=f"#{i + 1}", values=(act, summarize(rule)))
                self.iid_map[rid] = (path, i)
                self.rule_iid[(path, i)] = rid
        if select is not None:
            iid = self.rule_iid.get(select) if select[1] is not None else self.file_iid.get(select[0])
            if iid:
                t.selection_set(iid)
                t.see(iid)

    def _refresh_keep(self):
        sel = self._selected()
        self.refresh(select=sel)

    def _update_row(self, path, idx):
        st = self.files[path]
        rid = self.rule_iid.get((path, idx))
        if rid and self.tree.exists(rid):
            rule = st.rules[idx]
            act = rule.get("action") if isinstance(rule.get("action"), str) else "(no action)"
            self.tree.item(rid, values=(act, summarize(rule)))
        fid = self.file_iid.get(path)
        if fid and self.tree.exists(fid):
            self.tree.item(fid, text=self._file_label(st))

    def _show(self, target):
        if target is None:
            self.current = None
            self._set_editor_visible(False)
            self.placeholder_var.set("Select a rule to edit it.")
            return
        path, idx = target
        st = self.files.get(path)
        if idx is None or st is None:
            self.current = None
            self._set_editor_visible(False)
            if st is not None and st.error:
                self.placeholder_var.set(f"{self._rel(path)}\n\n{st.error}")
            elif st is not None:
                self.placeholder_var.set(f"{self._rel(path)}\n{len(st.rules)} rule(s)\n\n"
                                         f"Select a rule to edit it, or press \"New rule\".")
            return
        self.current = (path, idx)
        self.editor.load(st.rules[idx])
        self._set_editor_visible(True)

    def on_select(self, _=None):
        target = self._selected()
        if target == self.current and target is not None:
            return
        if not self._commit_current():
            if self.current:
                iid = self.rule_iid.get(self.current)
                if iid:
                    self.tree.selection_set(iid)
            return
        self._show(target)

    # ----- committing edits --------------------------------------------------
    def _commit_current(self):
        if self.current is None:
            return True
        path, idx = self.current
        st = self.files.get(path)
        if st is None or idx >= len(st.rules) or not self.editor.is_changed():
            return True
        try:
            rule = self.editor.get_rule()
        except ValueError as e:
            messagebox.showerror("Fix this first", str(e), parent=self)
            return False
        st.rules[idx] = rule
        st.dirty = True
        self.editor.mark_saved()
        self._update_row(path, idx)
        return True

    def _preview_commit(self):
        if self._commit_current():
            self.status("Edited in preview (not yet saved to disk)")

    def apply(self):
        if self._commit_current():
            self.status("Applied (not yet saved to disk)")

    # ----- rule / file operations ---------------------------------------------
    def new_file(self):
        if not self.folder:
            messagebox.showinfo("New file", "Choose a folder first.", parent=self)
            return
        if not self._commit_current():
            return
        name = simpledialog.askstring("New file", "File name (subfolders allowed, e.g. tools/swords.json):",
                                      initialvalue=self.p.new_file_name, parent=self)
        if not name:
            return
        name = name.strip().replace("\\", "/")
        if not name.lower().endswith(".json"):
            name += ".json"
        parts = [x for x in name.split("/") if x]
        path = os.path.abspath(os.path.join(self.folder, *parts))
        root = os.path.abspath(self.folder)
        try:
            inside = os.path.commonpath([root, path]) == root
        except ValueError:
            inside = False
        if not inside:
            messagebox.showerror("New file", "The file must be inside the folder.", parent=self)
            return
        if path in self.files:
            messagebox.showinfo("New file", "That file already exists - select it in the list.", parent=self)
            return
        st = FileState(path, [self.p.blank_rule()], False, new=True)
        self.files[path] = st
        self.refresh(select=(path, 0))
        self._show((path, 0))

    def new_rule(self):
        if not self._commit_current():
            return
        sel = self._selected()
        if sel is None:
            if len(self.files) == 1:
                sel = (next(iter(self.files)), None)
            elif not self.files:
                self.new_file()
                return
            else:
                messagebox.showinfo("New rule", "Select a file (or a rule inside it) first, or use \"New file\".",
                                    parent=self)
                return
        path, idx = sel
        st = self.files[path]
        if st.error:
            messagebox.showinfo("New rule", "That file couldn't be parsed, so it can't be edited here.", parent=self)
            return
        pos = len(st.rules) if idx is None else idx + 1
        st.rules.insert(pos, self.p.blank_rule())
        st.dirty = True
        self.current = None
        self.refresh(select=(path, pos))
        self._show((path, pos))

    def duplicate_rule(self):
        sel = self._selected_rule()
        if not sel or not self._commit_current():
            return
        path, idx = sel
        st = self.files[path]
        st.rules.insert(idx + 1, copy.deepcopy(st.rules[idx]))
        st.dirty = True
        self.current = None
        self.refresh(select=(path, idx + 1))
        self._show((path, idx + 1))

    def delete_rule(self):
        sel = self._selected_rule()
        if not sel:
            return
        path, idx = sel
        if not messagebox.askyesno("Delete rule", f"Delete rule #{idx + 1} from {self._rel(path)}?", parent=self):
            return
        st = self.files[path]
        del st.rules[idx]
        st.dirty = True
        self.current = None
        target = (path, min(idx, len(st.rules) - 1)) if st.rules else (path, None)
        self.refresh(select=target)
        self._show(target)

    def move_rule(self, delta):
        sel = self._selected_rule()
        if not sel or not self._commit_current():
            return
        path, idx = sel
        st = self.files[path]
        j = idx + delta
        if not 0 <= j < len(st.rules):
            return
        st.rules[idx], st.rules[j] = st.rules[j], st.rules[idx]
        st.dirty = True
        self.current = None
        self.refresh(select=(path, j))
        self._show((path, j))

    # ----- saving ---------------------------------------------------------------
    def _write(self, st):
        os.makedirs(os.path.dirname(st.path), exist_ok=True)
        if self.backup_var.get() and os.path.exists(st.path) and st.path not in self.backed_up:
            shutil.copy2(st.path, st.path + ".bak")
            self.backed_up.add(st.path)
        data = st.rules[0] if (st.single and len(st.rules) == 1) else st.rules
        tmp = st.path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, st.path)
        st.dirty = False

    def save_all(self):
        if not self._commit_current():
            return False
        problems = []
        for path, st in self.files.items():
            if st.dirty and not st.error:
                for i, rule in enumerate(st.rules):
                    for msg in self.p.validate(rule):
                        problems.append(f"{self._rel(path)}  #{i + 1}:  {msg}")
        if problems:
            shown = "\n".join(problems[:12]) + (f"\n...and {len(problems) - 12} more" if len(problems) > 12 else "")
            if not messagebox.askyesno("Check these rules", shown + "\n\nSave anyway?",
                                       icon="warning", default="no", parent=self):
                return False
        saved = 0
        for path, st in self.files.items():
            if st.dirty and not st.error:
                try:
                    self._write(st)
                    saved += 1
                except OSError as e:
                    messagebox.showerror("Save failed", f"{path}\n\n{e}", parent=self)
                    return False
        for path, fid in self.file_iid.items():
            if self.tree.exists(fid):
                self.tree.item(fid, text=self._file_label(self.files[path]))
        self.settings["backup"] = bool(self.backup_var.get())
        self.status(f"Saved {saved} file(s). Run /reload in-game to apply." if saved else "Nothing to save")
        return True


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Reliable Config Editor")
        self.geometry("1180x720")
        self.minsize(960, 560)
        self.settings = load_settings()
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=4, pady=4)
        self.tabs = []
        for prof in (RECIPES, REMOVER):
            tab = TabApp(self.nb, prof, self.settings, self._tab_loaded)
            self.nb.add(tab, text=prof.tab_title)
            self.tabs.append(tab)
        self.bind("<Control-s>", lambda e: self._current_tab().save_all())
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        for tab in self.tabs:
            if tab.folder_var.get().strip() and os.path.isdir(tab.folder_var.get().strip()):
                tab.load_folder(silent=True)

    def _current_tab(self):
        return self.tabs[self.nb.index(self.nb.select())]

    def _tab_loaded(self, tab, folder):
        """If the sibling mod's folder lives next to this one, load it too."""
        if os.path.basename(folder).lower() != tab.p.folder_name:
            return
        config_dir = os.path.dirname(folder)
        for other in self.tabs:
            if other is tab or other.folder_var.get().strip():
                continue
            cand = os.path.join(config_dir, other.p.folder_name)
            if os.path.isdir(cand):
                other.folder_var.set(cand)
                other.load_folder(silent=True)

    def on_close(self):
        for tab in self.tabs:
            if not tab._commit_current():
                return
        if any(tab.any_dirty() for tab in self.tabs):
            ans = messagebox.askyesnocancel("Unsaved changes", "Save your changes before closing?", parent=self)
            if ans is None:
                return
            if ans:
                for tab in self.tabs:
                    if not tab.save_all():
                        return
        save_settings(self.settings)
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
