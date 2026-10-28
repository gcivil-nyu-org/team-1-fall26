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
# Three decks, chosen per family. Saturday market is the default (key "primaries", kept so saved
# families keep working); Morning garden ("garden", soft) and Crayon box ("bold", strong) are the
# options. Each deck orders its colour families differently, so switching visibly recolours everyone.
# Each colour keeps its own natural lightness: step 3, the member's main chip, sits near the
# lightness where that hue is most vivid, so yellow stays light and bright, and rises only as far
# as dark text needs. Steps 1-2 are pale tints at a shared lightness; 4-5 go deep enough for
# white text. --x1t..--x5t name each step's text (--paint-ink or --on-paint-deep).
# Deep yellows turn orange instead of olive; greens and cyans stop short of the neon sRGB edge.
# Hues were searched so neighbouring members (a-b ... h-a) and the first four stay apart for
# protanopia, deuteranopia and tritanopia.
DECKS = {
    "primaries": (
        dict(l1=0.965, l2=0.905, l3=(0.66, 0.86), d4=0.15, d5=0.12, l4min=0.52, l5min=0.42,
             chroma=(0.030, 0.080, 0.175, 0.170, 0.140), share=(0.90, 0.92, 0.97, 0.97, 0.97)),
        (("Harbor blue", 264.9), ("Jade", 160.3), ("Sunflower", 91.7), ("Tomato", 30.9),
         ("Lagoon", 196.6), ("Grape", 307.5), ("Berry", 3.0), ("Lime", 128.9))),
    "garden": (
        # Morning garden: soft pastels. Lower chroma and a lighter main chip than the other decks,
        # with its own order of colour families, so switching to it changes every member's colour.
        dict(l1=0.970, l2=0.915, l3min=0.78, chroma=(0.024, 0.060, 0.130, 0.155, 0.125),
             share=(0.85, 0.85, 0.85, 0.90, 0.90)),
        (("Sage", 132.4), ("Plum", 329.6), ("Ochre", 81.7), ("Teal", 196.4),
         ("Lavender", 297.4), ("Terracotta", 33.7), ("Dusty blue", 265.1), ("Mint", 166.2))),
    "bold": (
        # Crayon box: full-strength colours in their own order of families.
        dict(l1=0.960, l2=0.895, l3min=0.70, chroma=(0.040, 0.110, 0.240, 0.220, 0.180),
             share=(0.95, 0.97, 1.00, 1.00, 1.00)),
        (("Cobalt", 258.2), ("Magenta", 352.4), ("Emerald", 162.1), ("Tangerine", 57.8),
         ("Violet", 303.1), ("Red", 36.6), ("Cyan", 210.6), ("Gold", 94.1))),
}
DECK_LABELS = {"primaries": "Saturday market", "garden": "Morning garden", "bold": "Crayon box"}
HUES = "abcdefgh"
INK_HEX = "#191714"     # --paint-ink (n20)
WHITE_HEX = "#ffffff"   # --on-paint-deep (n100)

# Picked colours (below) keep the older shared step lightness, so one rule covers any hue:
# steps 1-3 carry --paint-ink, 4-5 carry --on-paint-deep.
STEP_L = (0.955, 0.875, 0.775, 0.545, 0.435)

# A colour a parent picks is redrawn at the same step lightness with chroma capped per step.
# The caps are the largest chroma every hue can reach in sRGB at that lightness, so a picked
# colour never leaves the gamut and never changes weight.
CUSTOM_CAP = (0.021, 0.060, 0.113, 0.091, 0.074)

# --- Neutrals --------------------------------------------------------------------------------
# One warm ramp at the canvas hue: Saturday market's bright warm paper (chosen 2026-10-28).
# Names give the OKLCH lightness: --n98 is L 0.98.
NEUTRAL_HUE = 83
NEUTRALS = (
    ("n100", "#ffffff"),          # card stock (light card)
    ("n98", "#fbf7f0"),           # light counter (the canvas); dark ink
    ("n96", (0.960, 0.012)),      # light raised
    ("n92", "#ece3d4"),           # light line and track
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


def _cusp_l(h):
    """The OKLCH lightness where hue h reaches its largest in-gamut chroma."""
    best, at = 0.0, 0.5
    for i in range(30, 98):
        c = cm.max_chroma(i / 100, h)
        if c > best:
            best, at = c, i / 100
    return at


def _passes(text_hex, bg_hex, ratio=4.6, lc=61):
    t, b = cm.hex_to_rgba(text_hex), cm.hex_to_rgba(bg_hex)
    return cm.wcag_ratio(t, b) >= ratio and abs(cm.apca_lc(t, b)) >= lc


DARK_CARD = "#1f1d19"   # n23, the dark card a step-4 edge must show on


def ramp(params, hue):
    """Five hex steps for one member and the text each carries.

    Steps 1-2: pale tints at a shared lightness. Step 3, the member's main chip, starts at the
    lightness where the hue is most vivid and rises only until ink text passes (WCAG 4.5:1 and
    APCA Lc 60), so yellow, green and teal stay bright. Steps 4-5 carry white, except a yellow's or
    lime's step 4, which stays bright with ink. Step 5 also reads as text on step 1 (the quiet chip)
    and is the member's outline colour on light cards (step 3 on dark ones)."""
    yellow, cool = 60 <= hue <= 115, 120 <= hue <= 235
    share = [min(x, 0.85) for x in params["share"]] if cool else list(params["share"])

    def paint(k, L, h):
        return hexof(L, min(params["chroma"][k], cm.max_chroma(L, h) * share[k]), h)

    # Yellow tints need more chroma than other hues to read as yellow rather than sand.
    tint = 1.7 if yellow else 1.0
    s1 = hexof(params["l1"], min(params["chroma"][0] * tint, cm.max_chroma(params["l1"], hue) * share[0]), hue)
    s2 = hexof(params["l2"], min(params["chroma"][1] * tint, cm.max_chroma(params["l2"], hue) * share[1]), hue)
    # Step 3 stays at least 0.045 darker than the step-2 tint, so the steps read in order.
    L = min(max(_cusp_l(hue), params.get("l3min", 0.70)), 0.86 if cool else 0.90, params["l2"] - 0.045)
    s3 = paint(2, L, hue)
    while not _passes(INK_HEX, s3) and L < 0.97:
        L += 0.005
        s3 = paint(2, L, hue)
    # At the depth white text needs, yellow turns brown and lime turns olive. So their step 4
    # stays bright and carries ink (a marigold orange, a grass green), and only step 5 goes deep,
    # as burnt orange or a true green.
    lime = 115 < hue < 142
    texts = ["ink", "ink", "ink", "white", "white"]
    if yellow or lime:
        h4 = 68 if yellow else 138
        L = cm.rgb_to_oklch(cm.hex_to_rgba(s3))[0] - 0.09
        s4 = paint(3, L, h4)
        while not _passes(INK_HEX, s4) and L < 0.95:
            L += 0.005
            s4 = paint(3, L, h4)
        texts[3] = "ink"
        h5, L = (46 if yellow else 145), 0.52
    else:
        h4, L = hue, 0.575
        s4 = paint(3, L, h4)
        while not _passes(WHITE_HEX, s4) and L > 0.40:
            L -= 0.005
            s4 = paint(3, L, h4)
        h5, L = hue, min(L - 0.10, 0.47)
    s5 = paint(4, L, h5)
    while not (_passes(WHITE_HEX, s5) and _passes(s5, s1)) and L > 0.30:
        L -= 0.005
        s5 = paint(4, L, h5)
    return [s1, s2, s3, s4, s5], texts


def dark_step1(step5, card):
    """Dark mode draws quiet chips as 30% of step 5 over the card (paint.css); step 1 matches it."""
    return cm.rgb_to_hex(cm.mix_oklab(cm.hex_to_rgba(step5), cm.hex_to_rgba(card), 0.30))


def deck_values():
    """{deck: {letter: (name, steps, texts)}}"""
    out = {}
    for deck, (params, members) in DECKS.items():
        out[deck] = {}
        for i, (name, hue) in enumerate(members):
            steps, texts = ramp(params, hue)
            out[deck][HUES[i]] = (name, steps, texts)
    return out


def status(theme, name):
    return hexof(*STATUS[theme][name])


REGION_START = "/* Colour tokens."
REGION_END = "/* End of colour tokens. */"
SHELL = Path(__file__).resolve().parents[2] / "events/static/events/shell.css"


def _deck_block(deck, members):
    sel = ':root, [data-deck="primaries"]' if deck == "primaries" else f'[data-deck="{deck}"]'
    lines = [f"{sel} {{"]
    for h, (name, steps, texts) in members.items():
        lines.append("  " + " ".join(f"--{h}{k + 1}:{v};" for k, v in enumerate(steps)) + f"   /* {name} */")
        lines.append("    " + " ".join(
            f"--{h}{k + 1}t:var({'--paint-ink' if t == 'ink' else '--on-paint-deep'});" for k, t in enumerate(texts)))
    names = " ".join(f'--{h}-name:"{name}";' for h, (name, _, _) in members.items())
    lines.append(f"  {names}")
    return "\n".join(lines + ["}"])


def _dark_roles(st, n):
    d = st["dark"]
    return (
        "--counter:var(--n18); --card:var(--n23); --raised:var(--n27);\n"
        "--ink:var(--n98); --muted:var(--n80); --line:var(--n31); --control:var(--n56);\n"
        "--primary:var(--n98); --on-primary:var(--n18); --accent:var(--n98);\n"
        "--paint-ink:var(--n20); --on-paint-deep:var(--n100);\n"
        "--focus:var(--n98); --selection:var(--n31); --on-selection:var(--n98); --backdrop:#000000a3;\n"
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
    dark_s1 = {d: " ".join(f"--{h}1:{dark_step1(steps[4], n['n23'])};" for h, (_, steps, _) in m.items())
               for d, m in decks.items()}
    names = list(n)
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
 * The family's deck (Workspace.palette) is set as data-deck on <body> on every page; Saturday market
 * ("primaries") is the default, Morning garden ("garden") and Crayon box ("bold") are the options.
 * Each colour keeps its natural lightness, and --x1t..--x5t give the text each step carries
 * (--paint-ink or --on-paint-deep). Neighbouring members stay 10+ CIEDE2000 apart at steps 3-4
 * for protan, deutan and tritan vision. */
{_deck_block("primaries", decks["primaries"])}
{_deck_block("garden", decks["garden"])}
{_deck_block("bold", decks["bold"])}

/* Colour roles. Global, so accounts.css and checklists.css can use them on pages not yet ported. */
:root {{
  --card:var(--n100); --raised:var(--n96); --control:var(--n56);
  --primary:var(--n20); --on-primary:var(--n100);
  --paint-ink:var(--n20); --on-paint-deep:var(--n100); --fixed-white:var(--n100); --fixed-ink:var(--n20);
  --focus:var(--n20); --selection:var(--n80); --on-selection:var(--n20); --shadow:var(--n18); --backdrop:{n['n18']}66;
  --danger:{lt['danger']}; --danger-soft:{lt['danger-soft']}; --success:{lt['success']}; --success-soft:{lt['success-soft']};
  --warning:{lt['warning']}; --warning-soft:{lt['warning-soft']}; --info:{lt['info']}; --info-soft:{lt['info-soft']};
}}
.ui, body.paint {{
  --counter:var(--n98); --ink:var(--n20); --muted:var(--n47); --line:var(--n92);
  --track:{n['n92']}cc; --track-edge:#ffffff80; --glass-tint:#ffffff59; --glass-glow:#ffffff;
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
:root:has(body.paint) {{ color-scheme: light; --counter:var(--n98); }}
:root[data-theme="dark"]:has(body.paint) {{ color-scheme: dark; --counter:var(--n18); }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]):has(body.paint) {{ color-scheme: dark; --counter:var(--n18); }} }}

/* Member hue: set data-hue on any element and use --s1..--s5 (and --name, the colour's name) inside it. */
""" + "\n".join(f'[data-hue="{h}"] {{ --name:var(--{h}-name); ' + " ".join(f"--s{k}:var(--{h}{k}); --s{k}t:var(--{h}{k}t);" for k in range(1, 6)) + " }" for h in HUES) + f"""
/* A colour a parent picked (style="--hue:#rrggbb") is redrawn at the decks' step lightness, chroma capped
 * at what every hue reaches in sRGB, so any pick keeps the contrast the decks pass. Browsers without
 * relative colour syntax show the member's deck hue instead. */
@supports (color: oklch(from red l c h)) {{
  [data-hue][style*="--hue"] {{
    {caps[0]} {caps[1]}
    {caps[2]} {caps[3]}
    {caps[4]}
    --s1t:var(--paint-ink); --s2t:var(--paint-ink); --s3t:var(--paint-ink);
    --s4t:var(--on-paint-deep); --s5t:var(--on-paint-deep);
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
    print(f"OKLCH hue {hue}")
    for deck, (params, _) in DECKS.items():
        steps, texts = ramp(params, hue)
        print(f"  {DECK_LABELS[deck]:16s} " + " ".join(f"{c}({t})" for c, t in zip(steps, texts)))
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
