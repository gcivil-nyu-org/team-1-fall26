#!/usr/bin/env python3
"""Syncmily colour gate: measures the colour tokens that ship and fails on any miss.

Reads events/static/events/shell.css (and the older pages' :root tokens in
event_form.css), resolves every
role for light and dark and for each deck, then checks:

  1. contrast of every text/background and edge/background pair the stylesheets use
  (WCAG 2.x ratio and
     APCA Lc, thresholds by role: body text, small text, large text, UI edges);
  2. that every member ramp gets darker from step 1 to 5 (each colour keeps its own
  lightness), and dark step 1
     matches the dark quiet chip paint.css draws;
  3. that neighbouring members (a-b ... h-a) stay >= 10 CIEDE2000 apart at steps 3 and 4
  for normal vision
     and full protanopia, deuteranopia and tritanopia (Machado 2009), the first four
     members >= 6 apart
     in every one of those, and all members >= 12 apart for normal vision;
  4. that the dark theme's two blocks (theme switch and OS setting) hold the same
  values;
  5. that a colour a parent picks, any hue at any chroma, keeps the chip pairs passing.

    python docs/design/check_colours.py              # run the gates; exit 1 on any
    failure
    python docs/design/check_colours.py --verbose    # also list every passing pair
    python docs/design/check_colours.py --markdown   # contrast and CVD tables for
    colour.md
    python docs/design/check_colours.py --audit      # every colour literal per
    stylesheet and template

Standard library only (Python 3.9+).
"""

import argparse
import itertools
import math
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import colourmath as cm  # noqa: E402
import cssread  # noqa: E402

REPO = HERE.parents[1]
SHELL = REPO / "events/static/events/shell.css"
LEGACY = REPO / "events/static/events/event_form.css"
DECKS = ("primaries", "garden", "bold")
HUES = "abcdefgh"
SIMS = ("normal", "protan", "deutan", "tritan")

# role: (WCAG minimum, APCA |Lc| minimum or None)
KINDS = {
    "body": (4.5, 75),  # running text and headings on surfaces
    "text": (4.5, 60),  # small or secondary text: chips, labels, captions
    "large": (3.0, 45),  # 24px+ or 19px bold
    "ui": (3.0, None),  # edges and icons that identify a control (WCAG 1.4.11)
    "none": (1.0, None),  # measured and reported, no minimum
}
CVD_ADJACENT_MIN = 10.0
FIRST_FOUR_MIN = 6.0  # members a-d, any vision: most families have four people
NORMAL_ALL_MIN = 12.0
STEP_L_TOLERANCE = 0.006  # OKLCH L; 8-bit hex rounding alone moves L by up to ~0.003
NEUTRAL_HUE = 83  # the canvas hue
NEUTRAL_OFF_HUE = 0.0025  # OKLab distance from the canvas-hue line a neutral may sit

# --- Pairs the stylesheets use
# ----------------------------------------------------------------
# (what, where, foreground, background, kind). Expressions use CSS syntax plus over(A,
# B) for A
# painted on B and alpha(A, a). --s1..--s5 are filled in per member for the chip pairs.
PAINT_PAIRS = [
    ("Page text", "body.paint, h1, .empty", "var(--ink)", "var(--counter)", "body"),
    (
        "Card text",
        ".card .id b, .rail, .auth-form",
        "var(--ink)",
        "var(--card)",
        "body",
    ),
    (
        "Text on raised (hover rows, menus)",
        ".bar .nav a:hover, .acct-menu a:hover",
        "var(--ink)",
        "var(--raised)",
        "body",
    ),
    (
        "Muted on canvas",
        ".span, .days, .legend",
        "var(--muted)",
        "var(--counter)",
        "text",
    ),
    (
        "Muted on card",
        ".id small, .rail .sub, .row small, .note, .lede, helptext, placeholder",
        "var(--muted)",
        "var(--card)",
        "text",
    ),
    (
        "Muted on raised",
        ".palette-tab summary (open), menus",
        "var(--muted)",
        "var(--raised)",
        "text",
    ),
    (
        "Nav links on the bar track",
        ".bar .nav a",
        "var(--muted)",
        "over(var(--track), var(--card))",
        "text",
    ),
    (
        "Week switcher links on the canvas",
        ".seg a",
        "var(--muted)",
        "over(var(--track), var(--counter))",
        "text",
    ),
    (
        "Current week switcher pill",
        ".seg [aria-current]",
        "var(--ink)",
        "over(var(--glass-tint), over(var(--track), var(--counter)))",
        "text",
    ),
    ("Primary button", ".btn, .skip", "var(--on-primary)", "var(--primary)", "text"),
    (
        "Primary button, hover (88% opacity)",
        ".btn:hover",
        "over(alpha(var(--on-primary), .88), var(--counter))",
        "over(alpha(var(--primary), .88), var(--counter))",
        "text",
    ),
    (
        "Control edge on canvas",
        ".btn-quiet on .head",
        "var(--control)",
        "var(--counter)",
        "ui",
    ),
    (
        "Control edge on card",
        "inputs, selects, .btn-quiet",
        "var(--control)",
        "var(--card)",
        "ui",
    ),
    (
        "Control edge on raised",
        ".btn-quiet:hover",
        "var(--control)",
        "var(--raised)",
        "ui",
    ),
    ("Focus ring on canvas", ":focus-visible", "var(--focus)", "var(--counter)", "ui"),
    ("Focus ring on card", ":focus-visible", "var(--focus)", "var(--card)", "ui"),
    (
        "Switch icons on the track",
        ".bar .switch .track",
        "var(--muted)",
        "over(var(--track), var(--card))",
        "ui",
    ),
    (
        "Checkbox accent on card",
        "drivers.css accent-color",
        "var(--accent)",
        "var(--card)",
        "ui",
    ),
    ("Error text on card", ".errorlist", "var(--danger)", "var(--card)", "text"),
    (
        "Error text on canvas",
        ".errorlist outside cards",
        "var(--danger)",
        "var(--counter)",
        "text",
    ),
    (
        "Error flash",
        ".flash-error",
        "var(--danger)",
        "color-mix(in oklab, var(--danger) 8%, var(--card))",
        "text",
    ),
    (
        "Form error summary",
        ".errorlist.nonfield",
        "var(--danger)",
        "color-mix(in oklab, var(--danger) 10%, var(--card))",
        "text",
    ),
    ("Invalid input edge", "input[aria-invalid]", "var(--danger)", "var(--card)", "ui"),
    (
        "Danger on its tint",
        "--danger on --danger-soft",
        "var(--danger)",
        "var(--danger-soft)",
        "text",
    ),
    (
        "Success on its tint",
        "--success on --success-soft",
        "var(--success)",
        "var(--success-soft)",
        "text",
    ),
    (
        "Warning on its tint",
        "--warning on --warning-soft",
        "var(--warning)",
        "var(--warning-soft)",
        "text",
    ),
    (
        "Info on its tint",
        "--info on --info-soft",
        "var(--info)",
        "var(--info-soft)",
        "text",
    ),
    ("Success on card", "--success on --card", "var(--success)", "var(--card)", "text"),
    ("Warning on card", "--warning on --card", "var(--warning)", "var(--card)", "text"),
    ("Info on card", "--info on --card", "var(--info)", "var(--card)", "text"),
    (
        "Text on a filled status colour",
        "--card on --success/--danger",
        "var(--card)",
        "var(--success)",
        "text",
    ),
    (
        "Text on filled danger",
        "--card on --danger",
        "var(--card)",
        "var(--danger)",
        "text",
    ),
    (
        "Text on filled warning",
        "--card on --warning",
        "var(--card)",
        "var(--warning)",
        "text",
    ),
    ("Text on filled info", "--card on --info", "var(--card)", "var(--info)", "text"),
    (
        "Ink on its status tints",
        "flash text on --*-soft",
        "var(--ink)",
        "var(--warning-soft)",
        "body",
    ),
    (
        "Selection",
        "::selection (--selection)",
        "var(--on-selection)",
        "var(--selection)",
        "text",
    ),
    (
        "Fixed light panel (QR code)",
        "--fixed-ink on --fixed-white",
        "var(--fixed-ink)",
        "var(--fixed-white)",
        "body",
    ),
    (
        "Fixed light panel edge on canvas",
        "--fixed-white vs --counter",
        "var(--fixed-white)",
        "var(--counter)",
        "none",
    ),
    (
        "Clock switch label (fixed light look)",
        ".bar .clock-switch .lbl",
        "#4b504d",
        "#e3e7e4",
        "text",
    ),
    (
        "Clock switch current label",
        ".bar .clock-switch .lbl",
        "#181a19",
        "#f6f7f6",
        "text",
    ),
]

CHIP_PAIRS = {
    "light": [
        ("l0 quiet chip", ".l0", "var(--s5)", "var(--s1)", "text"),
        ("l1 chip", ".l1", "var(--paint-ink)", "var(--s2)", "text"),
        (
            "l2 chip, today rail event, timeline plan",
            ".l2, .ev, .plan, .kid-date",
            "var(--s3t)",
            "var(--s3)",
            "text",
        ),
        (
            "l3 chip, avatar dot, ticks",
            ".l3, .dot, .box, .who-pick",
            "var(--s4t)",
            "var(--s4)",
            "text",
        ),
        ("l4 chip", ".l4", "var(--s5t)", "var(--s5)", "text"),
        (
            "Step 3 accent edge on card",
            "kid hero and kid plan edge",
            "var(--s3)",
            "var(--card)",
            "none",
        ),
        (
            "Step 5 outline on card",
            "checkbox ring, unconfirmed dot",
            "var(--s5)",
            "var(--card)",
            "ui",
        ),
        ("Selected text (paint.css)", "::selection", "#111111", "var(--a2)", "text"),
    ],
    "dark": [
        (
            "l0 quiet chip (dark)",
            "[data-theme=dark] .l0",
            "var(--s2)",
            "color-mix(in oklab, var(--s5) 30%, var(--card))",
            "text",
        ),
        ("l1 chip", ".l1", "var(--paint-ink)", "var(--s2)", "text"),
        (
            "l2 chip, today rail event, timeline plan",
            ".l2, .ev, .plan, .kid-date",
            "var(--s3t)",
            "var(--s3)",
            "text",
        ),
        (
            "l3 chip, avatar dot, ticks",
            ".l3, .dot, .box, .who-pick",
            "var(--s4t)",
            "var(--s4)",
            "text",
        ),
        ("l4 chip", ".l4", "var(--s5t)", "var(--s5)", "text"),
        (
            "Step 3 accent edge on card",
            "kid hero and kid plan edge",
            "var(--s3)",
            "var(--card)",
            "none",
        ),
        (
            "Step 3 outline on dark card",
            "checkbox ring, unconfirmed dot",
            "var(--s3)",
            "var(--card)",
            "ui",
        ),
        (
            "Member pill (dark)",
            "[data-theme=dark] .pill[data-hue]",
            "var(--s2)",
            "color-mix(in oklab, var(--s5) 40%, var(--card))",
            "text",
        ),
        (
            "Who's going, not picked (dark)",
            "[data-theme=dark] .who-pick label",
            "var(--s2)",
            "var(--s1)",
            "text",
        ),
        ("Selected text (paint.css)", "::selection", "#111111", "var(--a2)", "text"),
    ],
}

# Pages not yet ported: accounts.css and checklists.css read the older pages'
# --ink/--muted/--line
# from event_form.css and the global roles from shell.css. Checked in that context and
# as if ported.
LEGACY_PAIRS = [
    ("Flash text", "accounts.css .flash", "var(--ink)", "var(--card)", "body"),
    (
        "Success flash",
        "accounts.css .flash-success",
        "var(--ink)",
        "var(--success-soft)",
        "body",
    ),
    (
        "Warning flash",
        "accounts.css .flash-warning",
        "var(--ink)",
        "var(--warning-soft)",
        "body",
    ),
    (
        "Error flash",
        "accounts.css .flash-error",
        "var(--ink)",
        "var(--danger-soft)",
        "body",
    ),
    (
        "Link button",
        "accounts.css .link-button",
        "var(--ink)",
        "var(--background, var(--counter))",
        "text",
    ),
    (
        "Role option edge",
        "accounts.css .role-option",
        "var(--control)",
        "var(--paper, var(--card))",
        "ui",
    ),
    (
        "Role option, checked",
        "accounts.css .role-option:checked",
        "var(--primary)",
        "var(--raised)",
        "ui",
    ),
    (
        "Role option text, checked",
        "accounts.css .role-option:checked",
        "var(--ink)",
        "var(--raised)",
        "body",
    ),
    (
        "Member secondary line",
        "accounts.css .member-name small",
        "var(--muted)",
        "var(--paper, var(--card))",
        "text",
    ),
    ("Kid avatar", "accounts.css .avatar-kid", "var(--ink)", "var(--raised)", "text"),
    (
        "Pending badge",
        "accounts.css .status-pending",
        "var(--warning)",
        "var(--warning-soft)",
        "text",
    ),
    (
        "Active badge",
        "accounts.css .status-active",
        "var(--success)",
        "var(--success-soft)",
        "text",
    ),
    (
        "Kid page text",
        "accounts.css .kid-dashboard",
        "var(--ink)",
        "var(--background, var(--counter))",
        "body",
    ),
    ("Kid hero", "accounts.css .kid-hero", "var(--ink)", "var(--card)", "body"),
    (
        "Kid page links",
        "accounts.css .kid-dashboard a",
        "var(--ink)",
        "var(--card)",
        "body",
    ),
    ("Kid today tile", "accounts.css .kid-today", "var(--ink)", "var(--card)", "body"),
    ("Kid event card", "accounts.css .kid-event", "var(--ink)", "var(--card)", "body"),
    (
        "Kid empty note",
        "accounts.css .kid-empty",
        "var(--ink)",
        "var(--raised)",
        "body",
    ),
    (
        "Kid family bubble",
        "accounts.css .kid-bubble",
        "var(--ink)",
        "var(--raised)",
        "large",
    ),
    (
        "Backup code",
        "accounts.css .backup-codes code",
        "var(--ink)",
        "var(--paper, var(--card))",
        "body",
    ),
    (
        "Checklist primary button",
        "checklists.css .cl-button",
        "var(--on-primary)",
        "var(--primary)",
        "text",
    ),
    (
        "Checklist primary button, hover",
        "checklists.css .cl-button:hover",
        "var(--on-primary)",
        "color-mix(in oklab, var(--primary) 88%, var(--card))",
        "text",
    ),
    (
        "Checklist danger button, hover",
        "checklists.css .cl-button-danger:hover",
        "var(--danger)",
        "color-mix(in oklab, var(--danger) 14%, var(--card))",
        "text",
    ),
    (
        "Done task toggle, hover",
        "checklists.css .cl-task-toggle:hover",
        "var(--success)",
        "var(--card)",
        "text",
    ),
    (
        "Checklist quiet button",
        "checklists.css .cl-button-secondary",
        "var(--ink)",
        "var(--card)",
        "text",
    ),
    (
        "Checklist quiet button edge",
        "checklists.css .cl-button-secondary",
        "var(--control)",
        "var(--card)",
        "ui",
    ),
    (
        "Checklist danger button",
        "checklists.css .cl-button-danger",
        "var(--danger)",
        "var(--danger-soft)",
        "text",
    ),
    (
        "Checklist card text",
        "checklists.css .cl-card",
        "var(--ink)",
        "var(--card)",
        "body",
    ),
    (
        "Checklist secondary text",
        "checklists.css .cl-muted, .cl-subtitle",
        "var(--muted)",
        "var(--card)",
        "text",
    ),
    (
        "Checklist secondary on page",
        "checklists.css .cl-subtitle",
        "var(--muted)",
        "var(--background, var(--counter))",
        "text",
    ),
    (
        "Checklist event tag",
        "checklists.css .cl-event",
        "var(--ink)",
        "var(--raised)",
        "text",
    ),
    (
        "Task count badge",
        "checklists.css .cl-task-count",
        "var(--muted)",
        "var(--card)",
        "text",
    ),
    (
        "Checklist links",
        "checklists.css .checklist-page a, .cl-back",
        "var(--ink)",
        "var(--background, var(--counter))",
        "body",
    ),
    (
        "Add-task field edge",
        "checklists.css .cl-add-field input",
        "var(--control)",
        "var(--card)",
        "ui",
    ),
    (
        "Progress bar fill",
        "checklists.css .cl-progress",
        "var(--success)",
        "var(--line)",
        "ui",
    ),
    (
        "Progress card number",
        "checklists.css .cl-progress-number",
        "var(--success)",
        "var(--success-soft)",
        "large",
    ),
    (
        "Progress card text",
        "checklists.css .cl-progress-card",
        "var(--ink)",
        "var(--success-soft)",
        "body",
    ),
    (
        "Done task text",
        "checklists.css .is-complete .cl-task-text",
        "var(--muted)",
        "var(--success-soft)",
        "text",
    ),
    (
        "Done by line",
        "checklists.css .cl-completed-by",
        "var(--muted)",
        "var(--success-soft)",
        "text",
    ),
    (
        "Checkbox edge",
        "checklists.css .cl-check-icon",
        "var(--control)",
        "var(--card)",
        "ui",
    ),
    (
        "Checked box tick",
        "checklists.css .is-complete .cl-check-icon",
        "var(--card)",
        "var(--success)",
        "text",
    ),
    (
        "Checked box on its row",
        "checklists.css .is-complete .cl-check-icon",
        "var(--success)",
        "var(--success-soft)",
        "ui",
    ),
    (
        "Task action links",
        "checklists.css .cl-task-actions a",
        "var(--ink)",
        "var(--card)",
        "text",
    ),
    (
        "Optional tag",
        "checklists.css .cl-optional",
        "var(--muted)",
        "var(--raised)",
        "text",
    ),
    (
        "Form field edge",
        "checklists.css .cl-field input",
        "var(--control)",
        "var(--card)",
        "ui",
    ),
    (
        "Form focus ring",
        "checklists.css .cl-field input:focus",
        "var(--focus)",
        "var(--card)",
        "ui",
    ),
]


# --- Token contexts
# ---------------------------------------------------------------------------


def _decls(rule):
    return {p: v for p, v, _ in rule.decls if p.startswith("--")}


def classify(rule):
    """Which token block a rule in shell.css is, or None."""
    sel, at = rule.selector, " ".join(rule.at)
    if not _decls(rule) or "data-hue" in sel or "@supports" in at:
        return None
    os_dark = "prefers-color-scheme: dark" in at or "prefers-color-scheme:dark" in at
    dark = '[data-theme="dark"]' in sel or os_dark
    decks = re.findall(r'\[data-deck="(\w+)"\]', sel)
    theme = ("dark-os" if os_dark else "dark") if dark else "light"
    if decks:
        return ("deck", theme, tuple(decks))
    if sel == ":root":
        return ("root", theme, ())
    if "body.paint" in sel or ".ui" in sel:
        return ("paint", theme, ())
    return None


def token_blocks(css_text):
    blocks = {}
    for rule in cssread.parse(css_text):
        kind = classify(rule)
        if kind:
            blocks.setdefault(kind, {}).update(_decls(rule))
    return blocks


def legacy_root(css_text):
    root = {}
    for rule in cssread.parse(css_text):
        if rule.selector == ":root" and not rule.at:
            root.update(_decls(rule))
    return root


def context(blocks, theme, deck, legacy=None):
    """Token values in effect for one theme ('light', 'dark', 'dark-os' or 'legacy') and
    deck.
    """
    tokens = dict(legacy or {})
    for (kind, th, decks), decls in blocks.items():
        if kind == "root" and th == "light":
            tokens.update(decls)
    for d in ("primaries", deck):
        for (kind, th, decks), decls in blocks.items():
            if kind == "deck" and th == "light" and d in decks:
                tokens.update(decls)
    if theme == "legacy":
        return tokens
    for (kind, th, decks), decls in blocks.items():
        if kind == "paint" and th == "light":
            tokens.update(decls)
    if theme in ("dark", "dark-os"):
        for (kind, th, decks), decls in blocks.items():
            if kind in ("paint", "root") and th == theme:
                tokens.update(decls)
        for (kind, th, decks), decls in blocks.items():
            if kind == "deck" and th == theme and deck in decks:
                tokens.update(decls)
    return tokens


# --- Evaluating CSS colour expressions
# ---------------------------------------------------------

NAMED = {
    "white": (1.0, 1.0, 1.0, 1.0),
    "black": (0.0, 0.0, 0.0, 1.0),
    "transparent": (0.0, 0.0, 0.0, 0.0),
}


def _split_args(text):
    out, depth, start = [], 0, 0
    for i, ch in enumerate(text):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append(text[start:i].strip())
            start = i + 1
    out.append(text[start:].strip())
    return out


def _func(expr):
    m = re.match(r"^([\w-]+)\((.*)\)$", expr.strip(), re.S)
    return (m.group(1).lower(), m.group(2)) if m else (None, None)


def evaluate(expr, tokens, depth=0):
    """CSS colour expression -> (r, g, b, a) in 0..1, or raise ValueError."""
    expr = expr.strip()
    if depth > 20:
        raise ValueError(f"var() loop at {expr}")
    if expr.startswith("#"):
        return cm.hex_to_rgba(expr)
    if expr.lower() in NAMED:
        return NAMED[expr.lower()]
    name, args = _func(expr)
    if name == "var":
        parts = _split_args(args)
        if parts[0] in tokens:
            return evaluate(tokens[parts[0]], tokens, depth + 1)
        if len(parts) > 1:
            return evaluate(",".join(parts[1:]), tokens, depth + 1)
        raise ValueError(f"{parts[0]} is not defined")
    if name == "over":
        top, base = _split_args(args)
        return cm.over(
            evaluate(top, tokens, depth + 1), evaluate(base, tokens, depth + 1)
        )
    if name == "alpha":
        col, a = _split_args(args)
        c = evaluate(col, tokens, depth + 1)
        return c[:3] + (c[3] * float(a),)
    if name == "color-mix":
        parts = _split_args(args)
        space = parts[0].replace("in", "").strip()
        (c1, p1), (c2, p2) = (_mix_part(p) for p in parts[1:3])
        if p1 is None and p2 is None:
            p1 = 0.5
        p1 = p1 if p1 is not None else 1 - p2
        a, b = evaluate(c1, tokens, depth + 1), evaluate(c2, tokens, depth + 1)
        if space == "oklab":
            return cm.mix_oklab(a, b, p1)
        if space == "srgb":
            return tuple(x * p1 + y * (1 - p1) for x, y in zip(a, b))
        raise ValueError(f"color-mix space {space}")
    if name in ("rgb", "rgba"):
        nums = re.split(r"[\s,/]+", args.strip())
        r, g, b = (
            float(x.rstrip("%")) / (100 if x.endswith("%") else 255) for x in nums[:3]
        )
        a = (
            float(nums[3].rstrip("%")) / (100 if nums[3].endswith("%") else 1)
            if len(nums) > 3
            else 1.0
        )
        return (r, g, b, a)
    if name == "oklch":
        nums = args.split()
        L = float(nums[0].rstrip("%")) / (100 if nums[0].endswith("%") else 1)
        return cm.oklch_to_rgb_clipped(L, float(nums[1]), float(nums[2])) + (1.0,)
    raise ValueError(f"cannot evaluate {expr!r}")


def _mix_part(text):
    m = re.match(r"^(.*?)\s+([\d.]+)%$", text.strip())
    return (m.group(1), float(m.group(2)) / 100) if m else (text.strip(), None)


def with_hue(tokens, letter):
    t = dict(tokens)
    for k in range(1, 6):
        t[f"--s{k}"] = f"var(--{letter}{k})"
        t[f"--s{k}t"] = f"var(--{letter}{k}t)"
    return t


# --- Checks
# -------------------------------------------------------------------------------------


class Report:
    def __init__(self, verbose):
        self.verbose, self.failures, self.passes, self.lines = verbose, [], 0, []

    def pair(self, label, fg, bg, kind, tokens):
        try:
            f, b = evaluate(fg, tokens), evaluate(bg, tokens)
        except ValueError as exc:
            self.fail(f"{label}: {exc}")
            return None
        if b[3] < 1:
            b = cm.over(b, (1, 1, 1, 1))
        if f[3] < 1:
            f = cm.over(f, b)
        ratio, lc = cm.wcag_ratio(f, b), cm.apca_lc(f, b)
        need_ratio, need_lc = KINDS[kind]
        ok = ratio >= need_ratio - 1e-9 and (need_lc is None or abs(lc) >= need_lc)
        row = (label, kind, cm.rgb_to_hex(f), cm.rgb_to_hex(b), ratio, lc, ok)
        if ok:
            self.passes += 1
            if self.verbose:
                self.lines.append(
                    f"  ok   {label}: {ratio:5.2f}:1  Lc {lc:6.1f}  ({kind})"
                )
        else:
            need = f"{need_ratio}:1" + (f", |Lc| {need_lc}" if need_lc else "")
            self.fail(
                f"{label}: {row[2]} on {row[3]} = {ratio:.2f}:1, Lc {lc:.1f} (needs"
                f" {need} for {kind})"
            )
        return row

    def check(self, label, ok, detail=""):
        if ok:
            self.passes += 1
            if self.verbose:
                self.lines.append(f"  ok   {label} {detail}")
        else:
            self.fail(f"{label} {detail}")

    def fail(self, message):
        self.failures.append(message)
        self.lines.append(f"  FAIL {message}")

    def section(self, title):
        self.lines.append(f"\n== {title}")


def check_pairs(report, blocks, legacy):
    rows = {}
    report.section("Contrast: chrome and text (ported pages)")
    for theme in ("light", "dark"):
        tokens = context(blocks, theme, "primaries")
        for what, where, fg, bg, kind in PAINT_PAIRS:
            row = report.pair(f"[{theme}] {what}", fg, bg, kind, tokens)
            if row:
                rows[(theme, what)] = (where,) + row
    report.section("Contrast: member chips (every deck, every member)")
    worst = {}
    for theme in ("light", "dark"):
        for deck in DECKS:
            base = context(blocks, theme, deck)
            for letter in HUES:
                tokens = with_hue(base, letter)
                for what, where, fg, bg, kind in CHIP_PAIRS[theme]:
                    if what.startswith("Selected text") and letter != "a":
                        continue
                    row = report.pair(
                        f"[{theme} {deck} {letter}] {what}", fg, bg, kind, tokens
                    )
                    if row:
                        key = (theme, what)
                        if key not in worst or row[4] < worst[key][6]:
                            worst[key] = (where, f"{deck} {letter}") + row
    report.section("Contrast: pages not yet ported (accounts.css, checklists.css)")
    legacy_rows = {}
    for theme in ("legacy", "light"):
        tokens = context(
            blocks, theme, "primaries", legacy if theme == "legacy" else None
        )
        for what, where, fg, bg, kind in LEGACY_PAIRS:
            label = f"[{'unported' if theme == 'legacy' else 'ported'}] {what}"
            row = report.pair(label, fg, bg, kind, tokens)
            if row:
                legacy_rows[(theme, what)] = (where,) + row
    return rows, worst, legacy_rows


def deck_colours(blocks, theme, deck):
    tokens = context(blocks, theme, deck)
    return {h: [evaluate(f"var(--{h}{k})", tokens) for k in range(1, 6)] for h in HUES}


def check_ramps(report, blocks):
    report.section("Member ramps: order, dark quiet step")
    for deck in DECKS:
        cols = deck_colours(blocks, "light", deck)
        for h in HUES:
            ls = [cm.rgb_to_oklch(c)[0] for c in cols[h]]
            gaps = [a - b for a, b in zip(ls, ls[1:])]
            olive = [
                k + 1
                for k in (3, 4)
                if 85 <= cm.rgb_to_oklch(cols[h][k])[2] <= 125
                and cm.rgb_to_oklch(cols[h][k])[0] < 0.62
            ]
            report.check(
                f"{deck} {h} deep steps are not olive or brown",
                not olive,
                f"(steps {olive})" if olive else "",
            )
            report.check(
                f"{deck} {h} steps darken in order",
                min(gaps) >= 0.04,
                f"(smallest step {min(gaps):.3f} L)",
            )
        for theme in ("dark", "dark-os"):
            tokens = context(blocks, theme, deck)
            for h in HUES:
                s1 = evaluate(f"var(--{h}1)", tokens)
                drawn = evaluate(
                    f"color-mix(in oklab, var(--{h}5) 30%, var(--card))", tokens
                )
                de = cm.delta_e_2000(cm.rgb_to_lab(s1), cm.rgb_to_lab(drawn))
                report.check(
                    f"[{theme}] {deck} {h}1 matches the quiet chip paint.css draws",
                    de < 1.5,
                    f"(dE {de:.2f})",
                )


def cvd_stats(cols, step):
    out = {}
    for sim in SIMS:
        lab = [cm.rgb_to_lab(cm.simulate(cols[h][step], sim)) for h in HUES]
        adj = [cm.delta_e_2000(lab[i], lab[(i + 1) % 8]) for i in range(8)]
        i = min(range(8), key=adj.__getitem__)
        allp = min(
            (cm.delta_e_2000(lab[i], lab[j]), HUES[i] + HUES[j])
            for i, j in itertools.combinations(range(8), 2)
        )
        four = min(
            (cm.delta_e_2000(lab[i], lab[j]), HUES[i] + HUES[j])
            for i, j in itertools.combinations(range(4), 2)
        )
        out[sim] = dict(
            adjacent=(adj[i], HUES[i] + HUES[(i + 1) % 8]), all=allp, first4=four
        )
    return out


def check_cvd(report, blocks):
    report.section("Colour-vision separation (CIEDE2000; Machado 2009, full severity)")
    table = {}
    for theme in ("light", "dark"):
        for deck in DECKS:
            cols = deck_colours(blocks, theme, deck)
            for step in (1, 2, 3, 4):
                stats = cvd_stats(cols, step)
                table[(theme, deck, step + 1)] = stats
                if step in (2, 3):
                    for sim in SIMS:
                        d, pair = stats[sim]["adjacent"]
                        report.check(
                            f"[{theme}] {deck} step {step + 1} {sim}: neighbours apart",
                            d >= CVD_ADJACENT_MIN,
                            f"(min {d:.1f} at {pair[0]}-{pair[1]}, needs"
                            f" {CVD_ADJACENT_MIN})",
                        )
                    d4, pair4 = min(stats[sim]["first4"] for sim in SIMS)
                    report.check(
                        f"[{theme}] {deck} step {step + 1}: first four members apart in"
                        " every vision",
                        d4 >= FIRST_FOUR_MIN,
                        f"(min {d4:.1f} at {pair4[0]}-{pair4[1]}, needs"
                        f" {FIRST_FOUR_MIN})",
                    )
                    d, pair = stats["normal"]["all"]
                    report.check(
                        f"[{theme}] {deck} step {step + 1} normal vision: all members"
                        " apart",
                        d >= NORMAL_ALL_MIN,
                        f"(min {d:.1f} at {pair[0]}-{pair[1]}, needs {NORMAL_ALL_MIN})",
                    )
    return table


def check_dark_parity(report, blocks):
    report.section("Dark theme: switch block and OS-setting block agree")
    for kind in ("paint", "root"):
        a, b = blocks.get((kind, "dark", ()), {}), blocks.get((kind, "dark-os", ()), {})
        if a or b:
            report.check(
                f"{kind} roles",
                a == b,
                (
                    ""
                    if a == b
                    else f"(differ: {sorted(set(a.items()) ^ set(b.items()))[:4]})"
                ),
            )
    for deck in DECKS:
        a = next(
            (
                v
                for (k, th, d), v in blocks.items()
                if k == "deck" and th == "dark" and deck in d
            ),
            {},
        )
        b = next(
            (
                v
                for (k, th, d), v in blocks.items()
                if k == "deck" and th == "dark-os" and deck in d
            ),
            {},
        )
        report.check(f"{deck} dark step 1", a == b and bool(a))


CUSTOM_RE = re.compile(
    r"--s(\d):\s*oklch\(from var\(--hue\)\s+([\d.]+)\s+min\(c,\s*([\d.]+)\)\s+h\)"
)


def check_custom(report, blocks, css_text):
    report.section("A colour a parent picks: every hue, every chroma")
    caps = {int(k): (float(L), float(c)) for k, L, c in CUSTOM_RE.findall(css_text)}
    if sorted(caps) != [1, 2, 3, 4, 5]:
        report.fail(
            "custom colour ramp (oklch(from var(--hue) ...)) not found for all five"
            " steps"
        )
        return
    worst = {}
    for theme in ("light", "dark"):
        tokens = context(blocks, theme, "primaries")
        for hue in range(0, 360, 5):
            for L0, C0 in (
                (0.45, 0.30),
                (0.62, 0.30),
                (0.80, 0.30),
                (0.62, 0.08),
                (0.95, 0.05),
                (0.30, 0.12),
            ):
                picked = cm.rgb_to_hex(cm.oklch_to_rgb_clipped(L0, C0, hue))
                pl, pc, ph = cm.rgb_to_oklch(cm.hex_to_rgba(picked))
                steps = [
                    cm.rgb_to_hex(
                        cm.oklch_to_rgb_clipped(caps[k][0], min(pc, caps[k][1]), ph)
                    )
                    for k in range(1, 6)
                ]
                t = dict(tokens, **{f"--s{k}": steps[k - 1] for k in range(1, 6)})
                t.update(
                    {
                        f"--s{k}t": (
                            "var(--paint-ink)" if k <= 3 else "var(--on-paint-deep)"
                        )
                        for k in range(1, 6)
                    }
                )
                if theme == "dark":
                    t["--s1"] = "color-mix(in oklab, var(--s5) 30%, var(--card))"
                for what, where, fg, bg, kind in CHIP_PAIRS[theme][:7]:
                    f, b = evaluate(fg, t), evaluate(bg, t)
                    ratio, lc = cm.wcag_ratio(f, b), cm.apca_lc(f, b)
                    need_ratio, need_lc = KINDS[kind]
                    key = (theme, what)
                    if key not in worst or ratio < worst[key][0]:
                        worst[key] = (ratio, lc, picked)
                    if ratio < need_ratio or (need_lc and abs(lc) < need_lc):
                        report.fail(
                            f"[{theme}] picked {picked}: {what} {ratio:.2f}:1 Lc"
                            f" {lc:.1f}"
                        )
    for (theme, what), (ratio, lc, picked) in sorted(worst.items()):
        report.check(
            f"[{theme}] picked colours, {what}",
            True,
            f"(worst {ratio:.2f}:1, Lc {lc:.1f}, from {picked})",
        )
    return worst


def check_neutrals(report, blocks):
    report.section("Neutral ramp: one warm hue, lighter to darker by name")
    root = next(
        (
            v
            for (k, th, d), v in blocks.items()
            if k == "root" and th == "light" and "--n100" in v
        ),
        {},
    )
    names = sorted(
        (n for n in root if re.fullmatch(r"--n\d+", n)), key=lambda n: -int(n[3:])
    )
    ls = [cm.rgb_to_oklch(cm.hex_to_rgba(root[n]))[0] for n in names]
    report.check(
        "neutral names follow lightness",
        all(a > b for a, b in zip(ls, ls[1:])) and bool(names),
    )
    for n in names:
        L, C, h = cm.rgb_to_oklch(cm.hex_to_rgba(root[n]))
        named = int(n[3:]) / 100
        report.check(
            f"{n} lightness matches its name", abs(L - named) < 0.012, f"(L {L:.3f})"
        )
        if C > 0.002:
            dh = math.radians(h - NEUTRAL_HUE)
            off = abs(C * math.sin(dh))
            report.check(
                f"{n} sits on the canvas hue",
                off <= NEUTRAL_OFF_HUE and math.cos(dh) > 0,
                f"(h {h:.1f}, C {C:.4f}, {off:.4f} off the h {NEUTRAL_HUE} line)",
            )


# --- Audit
# -------------------------------------------------------------------------------------


def audit():
    blocks = token_blocks(SHELL.read_text())
    light = context(blocks, "light", "primaries")
    role_names = [n for n in light if not re.fullmatch(r"--([a-h]\d|n\d+|s\d)", n)]
    role_vals = {}
    for n in role_names:
        try:
            v = evaluate(f"var({n})", light)
        except ValueError:
            continue
        if v[3] == 1:
            role_vals[n] = v
    print(
        "Colour literals in declarations (font properties skipped). Token blocks at the"
        " top of shell.css are"
    )
    print("the palette itself; everything else is a hard-coded colour.\n")
    files = sorted(
        p
        for p in REPO.rglob("*.css")
        if ".git" not in p.parts and "staticfiles" not in p.parts
    )
    grand = 0
    for path in files:
        rel = path.relative_to(REPO)
        hard = []
        for rule in cssread.parse(path.read_text()):
            tokens_block = rel == SHELL.relative_to(REPO) and classify(rule) is not None
            for prop, value, line in rule.decls:
                if (
                    prop.startswith("font")
                    or tokens_block
                    or (prop.startswith("--") and "data-hue" in rule.selector)
                ):
                    continue
                for lit in cssread.colour_literals(value):
                    hard.append((line, prop, lit, rule.selector))
        grand += len(hard)
        print(f"{rel}: {len(hard)} hard-coded")
        for line, prop, lit, sel in hard:
            hint = ""
            if lit.startswith("#") and len(lit.lstrip("#")) in (3, 6):
                c = cm.hex_to_rgba(lit)
                name, de = min(
                    (
                        (n, cm.delta_e_2000(cm.rgb_to_lab(c), cm.rgb_to_lab(v)))
                        for n, v in role_vals.items()
                    ),
                    key=lambda x: x[1],
                )
                hint = f"  nearest role {name} (dE {de:.1f})"
            print(f"  {line:4d} {prop:20s} {lit:26s}{hint}   {sel[:48]}")
    print(f"\nTotal hard-coded colours in stylesheets: {grand}")
    print("\nTemplates and scripts:")
    for path in sorted(
        p
        for p in REPO.rglob("*")
        if p.suffix in (".html", ".js") and ".git" not in p.parts
    ):
        text = path.read_text(errors="ignore")
        found = []
        for m in re.finditer(r'style="([^"]*)"', text):
            found += cssread.colour_literals(m.group(1))
        for m in re.finditer(r"<style>(.*?)</style>", text, re.S):
            for rule in cssread.parse(m.group(1)):
                for prop, value, line in rule.decls:
                    found += cssread.colour_literals(value)
        if path.suffix == ".js":
            found += re.findall(
                r"['\"](#[0-9a-fA-F]{3,8})['\"]|(rgba?\([^)]*\))", text
            ) and [
                a or b
                for a, b in re.findall(
                    r"['\"](#[0-9a-fA-F]{3,8})['\"]|(rgba?\([^)]*\))", text
                )
            ]
        svg = re.findall(r'(?:fill|stroke)="(#[0-9a-fA-F]{3,8})"', text)
        if found or svg:
            note = f" (+{len(svg)} SVG brand fills)" if svg else ""
            print(f"  {path.relative_to(REPO)}: {len(found)}{note}")


# --- Output
# ------------------------------------------------------------------------------------


def markdown(rows, worst, legacy_rows, table, custom_worst):
    print(
        "| Pair | Where | Theme | Text / edge | Background | WCAG | APCA Lc | Needs |"
    )
    print("|---|---|---|---|---|---|---|---|")
    for (theme, what), (where, label, kind, f, b, ratio, lc, ok) in rows.items():
        need = KINDS[kind]
        print(
            f"| {what} | `{where}` | {theme} | `{f}` | `{b}` | {ratio:.2f} | {lc:.0f} |"
            f" {need[0]}:1" + (f", Lc {need[1]}" if need[1] else "") + " |"
        )
    print("\nMember chips, worst member in any deck:\n")
    print(
        "| Chip | Theme | Worst member | Text | Background | WCAG | APCA Lc | Needs |"
    )
    print("|---|---|---|---|---|---|---|---|")
    for (theme, what), (where, who, label, kind, f, b, ratio, lc, ok) in worst.items():
        need = KINDS[kind]
        print(
            f"| {what} | {theme} | {who} | `{f}` | `{b}` | {ratio:.2f} | {lc:.0f} |"
            f" {need[0]}:1" + (f", Lc {need[1]}" if need[1] else "") + " |"
        )
    print(
        "\nPages not yet ported (left: with the older pages' tokens; right: once"
        " ported):\n"
    )
    print("| Pair | Where | Unported WCAG / Lc | Ported WCAG / Lc | Needs |")
    print("|---|---|---|---|---|")
    for what, where, fg, bg, kind in LEGACY_PAIRS:
        a, b = legacy_rows.get(("legacy", what)), legacy_rows.get(("light", what))
        need = KINDS[kind]
        cell = lambda r: f"{r[5]:.2f} / {r[6]:.0f}" if r else "n/a"  # noqa: E731
        print(
            f"| {what} | `{where}` | {cell(a)} | {cell(b)} | {need[0]}:1"
            + (f", Lc {need[1]}" if need[1] else "")
            + " |"
        )
    print("\nColour-vision separation, CIEDE2000 (minimum over the deck):\n")
    print(
        "| Deck | Theme | Step | Vision | Neighbours (min, pair) | All 8 members |"
        " First 4 members |"
    )
    print("|---|---|---|---|---|---|---|")
    for (theme, deck, step), stats in table.items():
        if step not in (3, 4):
            continue
        for sim in SIMS:
            s = stats[sim]
            print(
                f"| {deck} | {theme} | {step} | {sim} | {s['adjacent'][0]:.1f}"
                f" ({s['adjacent'][1][0]}-{s['adjacent'][1][1]}) | {s['all'][0]:.1f}"
                f" ({s['all'][1][0]}-{s['all'][1][1]}) | {s['first4'][0]:.1f}"
                f" ({s['first4'][1][0]}-{s['first4'][1][1]}) |"
            )
    if custom_worst:
        print("\nPicked colours (432 picks x 2 themes), worst case:\n")
        print("| Chip | Theme | WCAG | APCA Lc | Picked |")
        print("|---|---|---|---|---|")
        for (theme, what), (ratio, lc, picked) in sorted(custom_worst.items()):
            print(f"| {what} | {theme} | {ratio:.2f} | {lc:.0f} | `{picked}` |")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--verbose", action="store_true", help="list passing checks too"
    )
    parser.add_argument(
        "--markdown", action="store_true", help="print the tables used in colour.md"
    )
    parser.add_argument(
        "--audit",
        action="store_true",
        help="list every colour literal in stylesheets and templates",
    )
    parser.add_argument(
        "--css",
        type=Path,
        default=SHELL,
        help="shell.css to check (default: the repo's)",
    )
    args = parser.parse_args()
    if args.audit:
        audit()
        return 0
    css_text = args.css.read_text()
    blocks = token_blocks(css_text)
    legacy = legacy_root(LEGACY.read_text()) if LEGACY.exists() else {}
    report = Report(args.verbose)
    rows, worst, legacy_rows = check_pairs(report, blocks, legacy)
    check_ramps(report, blocks)
    table = check_cvd(report, blocks)
    check_dark_parity(report, blocks)
    custom_worst = check_custom(report, blocks, css_text)
    check_neutrals(report, blocks)
    if args.markdown:
        markdown(rows, worst, legacy_rows, table, custom_worst)
        return 0 if not report.failures else 1
    for line in report.lines:
        if args.verbose or line.startswith("\n==") or "FAIL" in line:
            print(line)
    print("\nCVD minima (CIEDE2000, neighbouring members, steps 3-4):")
    for deck in DECKS:
        for theme in ("light", "dark"):
            mins = {
                sim: min(table[(theme, deck, s)][sim]["adjacent"][0] for s in (3, 4))
                for sim in SIMS
            }
            four = min(
                table[(theme, deck, s)][sim]["first4"][0]
                for s in (3, 4)
                for sim in SIMS
            )
            print(
                f"  {deck:9s} {theme:5s} "
                + "  ".join(f"{k} {v:5.1f}" for k, v in mins.items())
                + f"   (first four, any vision: {four:.1f})"
            )
    print(f"\n{report.passes} checks passed, {len(report.failures)} failed.")
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
