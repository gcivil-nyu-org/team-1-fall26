#!/usr/bin/env python3
"""Syncmily palette source: the colour system written in OKLCH, printed as the hex tokens in shell.css.

    python docs/design/palette.py               # print the colour-token region of events/static/events/shell.css
    python docs/design/palette.py --write       # rewrite that region in place (only between its two markers)
    python docs/design/palette.py --ramp 200    # a five-step member ramp for OKLCH hue 200 (adding a colour)

Every value is derived here, so a change is one edit plus a re-run of check_colours.py.
Standard library only.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import colourmath as cm  # noqa: E402

# --- Member decks --------------------------------------------------------------------------
# Step lightness is shared by every member of every deck: a chip's depth reads the same for
# everyone. 1-3 carry --paint-ink (near-black) text, 4-5 carry white; 0.775 keeps ink at APCA
# Lc 60+ and 0.535 keeps white at WCAG 4.5:1+ and Lc 75.
STEP_L = (0.955, 0.875, 0.775, 0.545, 0.435)

# 0.545 at step 4 is the narrow band where white text keeps 4.5:1 on the most luminous hue (green) and
# the least luminous (magenta, red) still shows a 3:1 edge on the dark card.
# Per deck: chroma per step (the deck's character), then members a..h as
# (name, OKLCH hue, chroma multiplier). Yellows need a multiplier above 1: at a shared lightness the
# same chroma that makes blue vivid leaves yellow khaki. Hues were searched so that neighbouring members
# (a-b, b-c, ... h-a) stay >= 10 CIEDE2000 apart at steps 3 and 4 for normal vision and full
# protanopia, deuteranopia and tritanopia, while a-d stay inside their approved colour family.
DECKS = {
    "primaries": ((0.032, 0.075, 0.130, 0.155, 0.130), (
        ("Harbor blue", 264.2, 0.91), ("Fern", 155.1, 1.00), ("Marigold", 96.2, 1.35),
        ("Coral", 18.7, 0.84), ("Violet", 304.8, 0.87), ("Teal", 189.7, 0.89),
        ("Leaf", 122.1, 0.91), ("Magenta", 349.5, 0.93))),
    "garden": ((0.026, 0.058, 0.095, 0.105, 0.088), (
        ("Sage", 152.3, 0.74), ("Plum", 318.6, 0.84), ("Ochre", 78.2, 1.00),
        ("Teal", 194.0, 0.92), ("Lavender", 279.3, 1.00), ("Terracotta", 44.6, 0.95),
        ("Dusty blue", 243.8, 1.00), ("Moss", 110.0, 1.00))),
    "bold": ((0.040, 0.100, 0.160, 0.190, 0.155), (
        ("Cobalt", 267.4, 1.00), ("Magenta", 352.1, 1.00), ("Emerald", 161.0, 0.92),
        ("Tangerine", 53.6, 1.00), ("Violet", 300.9, 0.90), ("Red", 26.4, 0.95),
        ("Sky", 237.2, 1.00), ("Gold", 92.6, 0.90))),
}
# Chroma never goes past this share of the sRGB limit at each step: colours stop short of the
# gamut edge, where cyans and greens turn neon.
GAMUT_SHARE = (0.90, 0.85, 0.90, 0.95, 0.95)
HUES = "abcdefgh"

# A colour a parent picks is redrawn at the same step lightness with chroma capped per step.
# The caps are the largest chroma every hue can reach in sRGB at that lightness, so a picked
# colour never leaves the gamut and never changes weight.
CUSTOM_CAP = (0.021, 0.060, 0.113, 0.091, 0.074)

# --- Neutrals --------------------------------------------------------------------------------
# One warm ramp at the canvas hue. The three canvas values are the ones chosen on 2026-10-27
# and are kept as they are. Names give the OKLCH lightness: --n94 is L 0.94.
NEUTRAL_HUE = 83
NEUTRALS = (
    ("n100", "#ffffff"),          # card stock (light card)
    ("n97", "#f8f4ec"),           # light raised; dark ink
    ("n94", "#f2ece1"),           # light counter (the warm canvas)
    ("n90", "#e6ded0"),           # light line and track
    ("n80", (0.800, 0.013)),      # dark muted text; selection
    ("n56", (0.560, 0.013)),      # control edges, both themes (3:1 on every surface)
    ("n47", (0.470, 0.013)),      # light muted text
    ("n31", (0.310, 0.010)),      # dark line
    ("n27", (0.270, 0.009)),      # dark raised
    ("n23", (0.232, 0.008)),      # dark card
    ("n20", (0.205, 0.007)),      # light ink and primary
    ("n18", (0.180, 0.006)),      # dark counter; shadow
)

# --- Status colours: (L, C, h) per theme --------------------------------------------------
STATUS = {
    "light": {
        "danger": (0.501, 0.178, 28.7), "danger-soft": (0.962, 0.020, 28),
        "success": (0.495, 0.105, 152), "success-soft": (0.962, 0.020, 152),
        "warning": (0.515, 0.105, 70), "warning-soft": (0.962, 0.040, 88),
        "info": (0.500, 0.120, 256), "info-soft": (0.962, 0.020, 256),
    },
    "dark": {
        "danger": (0.800, 0.115, 25), "danger-soft": (0.280, 0.045, 28),
        "success": (0.800, 0.115, 152), "success-soft": (0.280, 0.040, 152),
        "warning": (0.830, 0.110, 82), "warning-soft": (0.285, 0.040, 78),
        "info": (0.800, 0.095, 256), "info-soft": (0.280, 0.040, 256),
    },
}


def hexof(L, C, h):
    return cm.rgb_to_hex(cm.oklch_to_rgb(L, C, h))


def neutral(name):
    value = dict(NEUTRALS)[name]
    return value if isinstance(value, str) else hexof(value[0], value[1], NEUTRAL_HUE)


def deep_hue(hue):
    """Yellows warm toward amber in steps 4-5, as ochre pigments do; at equal lightness a deep
    yellow kept at its own hue turns olive."""
    return hue - 0.6 * (hue - 72) if 78 <= hue <= 100 else hue


def ramp(chroma, hue, mult=1.0):
    """Five hex steps for one member: shared lightness, chroma capped below the sRGB edge."""
    out = []
    for k, (L, C) in enumerate(zip(STEP_L, chroma)):
        h = hue if k < 3 else deep_hue(hue)
        out.append(hexof(L, min(C * mult, cm.max_chroma(L, h) * GAMUT_SHARE[k]), h))
    return out


def dark_step1(step5, card):
    """Dark mode draws quiet chips as 30% of step 5 over the card (paint.css); step 1 matches it."""
    return cm.rgb_to_hex(cm.mix_oklab(cm.hex_to_rgba(step5), cm.hex_to_rgba(card), 0.30))


def deck_values():
    return {deck: {HUES[i]: (name, ramp(chroma, hue, mult)) for i, (name, hue, mult) in enumerate(members)}
            for deck, (chroma, members) in DECKS.items()}


def status(theme, name):
    return hexof(*STATUS[theme][name])


REGION_START = "/* Colour tokens."
REGION_END = "/* End of colour tokens. */"
SHELL = Path(__file__).resolve().parents[2] / "events/static/events/shell.css"


def _deck_block(deck, members):
    sel = ':root, [data-deck="primaries"]' if deck == "primaries" else f'[data-deck="{deck}"]'
    lines = [f"{sel} {{"]
    for h, (name, steps) in members.items():
        lines.append("  " + " ".join(f"--{h}{k + 1}:{v};" for k, v in enumerate(steps)) + f"   /* {name} */")
    return "\n".join(lines + ["}"])


def _dark_roles(st, n):
    d = st["dark"]
    return (
        "--counter:var(--n18); --card:var(--n23); --raised:var(--n27);\n"
        "--ink:var(--n97); --muted:var(--n80); --line:var(--n31); --control:var(--n56);\n"
        "--primary:var(--n97); --on-primary:var(--n18); --accent:var(--n97);\n"
        "--paint-ink:var(--n20); --on-deep:var(--n100);\n"
        "--focus:var(--n97); --selection:var(--n31); --on-selection:var(--n97);\n"
        f"--danger:{d['danger']}; --danger-soft:{d['danger-soft']}; --success:{d['success']}; --success-soft:{d['success-soft']};\n"
        f"--warning:{d['warning']}; --warning-soft:{d['warning-soft']}; --info:{d['info']}; --info-soft:{d['info-soft']};\n"
        f"--track:{n['n27']}cc; --track-edge:#ffffff14; --glass-tint:#ffffff1f; --glass-glow:#ffffff59;"
    )


def _indent(text, pad):
    return "\n".join(pad + line for line in text.split("\n"))


def css_region():
    """The colour-token region of shell.css, from REGION_START to REGION_END."""
    decks = deck_values()
    n = {name: neutral(name) for name, _ in NEUTRALS}
    st = {t: {k: status(t, k) for k in STATUS[t]} for t in STATUS}
    lt = st["light"]
    dark_s1 = {d: " ".join(f"--{h}1:{dark_step1(steps[4], n['n23'])};" for h, (_, steps) in m.items()) for d, m in decks.items()}
    names = list(n)
    L = " ".join(f"{x:g}"[1:] if x < 1 else f"{x:g}" for x in STEP_L)
    caps = [f"--s{k + 1}:oklch(from var(--hue) {f'{STEP_L[k]:g}'[1:]} min(c, {f'{CUSTOM_CAP[k]:g}'[1:]}) h);" for k in range(5)]
    dark_sel = '[data-theme="dark"] body.paint, [data-theme="dark"] body.paint .ui'
    os_sel = ':root:not([data-theme="light"]) body.paint, :root:not([data-theme="light"]) body.paint .ui'
    out = f"""{REGION_START} Values come from docs/design/palette.py (OKLCH in, hex out; --write regenerates this region);
 * docs/design/check_colours.py measures every pair the pages use (WCAG 2.x and APCA) and colour-blind separation,
 * and fails on a miss. Roles, values and how to add a colour: docs/design/colour.md. Components use the roles,
 * not the --nNN ramp. */

/* Neutrals: one warm ramp at the canvas hue (OKLCH h {NEUTRAL_HUE}). The number is the OKLCH lightness. */
:root {{
  {" ".join(f"--{k}:{n[k]};" for k in names[:6])}
  {" ".join(f"--{k}:{n[k]};" for k in names[6:])}
}}

/* Fan decks: eight hue ramps each, step 1 (palest) to step 5 (deepest). Members get a..h in family order.
 * The family's deck (Workspace.palette) is set as data-deck on <body>; Primaries is the default.
 * Every ramp shares the step lightness (OKLCH L {L}), so a chip's depth reads the same weight
 * whoever it belongs to. Steps 1-3 carry --paint-ink, 4-5 carry --on-deep (white), and step 5 reads on step 1.
 * Neighbouring members stay 10+ CIEDE2000 apart at steps 3-4 for protan, deutan and tritan vision. */
{_deck_block("primaries", decks["primaries"])}
{_deck_block("garden", decks["garden"])}
{_deck_block("bold", decks["bold"])}

/* Colour roles. Global, so accounts.css and checklists.css can use them on pages not yet ported. */
:root {{
  --card:var(--n100); --raised:var(--n97); --control:var(--n56);
  --primary:var(--n20); --on-primary:var(--n100);
  --paint-ink:var(--n20); --on-deep:var(--n100);
  --focus:var(--n20); --selection:var(--n80); --on-selection:var(--n20); --shade:var(--n18);
  --danger:{lt['danger']}; --danger-soft:{lt['danger-soft']}; --success:{lt['success']}; --success-soft:{lt['success-soft']};
  --warning:{lt['warning']}; --warning-soft:{lt['warning-soft']}; --info:{lt['info']}; --info-soft:{lt['info-soft']};
}}
.ui, body.paint {{
  --counter:var(--n94); --ink:var(--n20); --muted:var(--n47); --line:var(--n90);
  --track:{n['n90']}cc; --track-edge:#ffffff80; --glass-tint:#ffffff59; --glass-glow:#ffffff;
  --radius:4px; --radius-control:6px;
  --font:"Schibsted Grotesk", system-ui, sans-serif;
}}
body.paint {{ --accent:var(--n20); }} /* drivers.css checkboxes */

/* Dark: ported pages only. The theme switch sets data-theme (theme_boot.js, before first paint); with
 * scripts off the OS setting applies. Both blocks hold the same values (check_colours.py compares them).
 * Chips keep their paint, so a member's colour is the same at night; quiet chips (step 1) dim into the card. */
{dark_sel} {{
{_indent(_dark_roles(st, n), "  ")}
}}
""" + "\n".join(f'[data-theme="dark"] body.paint[data-deck="{d}"] {{ {dark_s1[d]} }}' for d in decks) + f"""
@media (prefers-color-scheme: dark) {{
  {os_sel} {{
{_indent(_dark_roles(st, n), "    ")}
  }}
""" + "\n".join(f'  :root:not([data-theme="light"]) body.paint[data-deck="{d}"] {{ {dark_s1[d]} }}' for d in decks) + f"""
}}
/* On the root so scrollbars, form controls and the overscroll area follow the theme too. */
:root:has(body.paint) {{ color-scheme: light; --counter:var(--n94); }}
:root[data-theme="dark"]:has(body.paint) {{ color-scheme: dark; --counter:var(--n18); }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]):has(body.paint) {{ color-scheme: dark; --counter:var(--n18); }} }}

/* Member hue: set data-hue on any element and use --s1..--s5 inside it. */
""" + "\n".join(f'[data-hue="{h}"] {{ ' + " ".join(f"--s{k}:var(--{h}{k});" for k in range(1, 6)) + " }" for h in HUES) + f"""
/* A colour a parent picked (style="--hue:#rrggbb") is redrawn at the decks' step lightness, chroma capped
 * at what every hue reaches in sRGB, so any pick keeps the contrast the decks pass. Browsers without
 * relative colour syntax show the member's deck hue instead. */
@supports (color: oklch(from red l c h)) {{
  [data-hue][style*="--hue"] {{
    {caps[0]} {caps[1]}
    {caps[2]} {caps[3]}
    {caps[4]}
  }}
  [data-theme="dark"] body.paint [data-hue][style*="--hue"] {{ --s1:color-mix(in oklab, var(--s5) 30%, var(--card)); }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) body.paint [data-hue][style*="--hue"] {{ --s1:color-mix(in oklab, var(--s5) 30%, var(--card)); }}
  }}
}}
{REGION_END}"""
    return out


def write_region(path=SHELL):
    text = path.read_text()
    a = text.index(REGION_START)
    b = text.index(REGION_END) + len(REGION_END)
    path.write_text(text[:a] + css_region() + text[b:])
    print(f"Rewrote the colour-token region of {path}")


def print_ramp(hue):
    print(f"OKLCH hue {hue}: shared step lightness {STEP_L}")
    for deck, (chroma, _) in DECKS.items():
        steps = ramp(chroma, hue)
        print(f"  {deck:10s} " + " ".join(steps))
    print("Paste one row as --x1..--x5 in the deck, then run docs/design/check_colours.py.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ramp", type=float, metavar="HUE", help="print a member ramp for an OKLCH hue (0-360)")
    parser.add_argument("--write", action="store_true", help="rewrite the colour-token region of shell.css")
    args = parser.parse_args()
    if args.ramp is not None:
        print_ramp(args.ramp % 360)
    elif args.write:
        write_region()
    else:
        print(css_region())
