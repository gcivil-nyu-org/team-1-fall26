# Colour

Syncmily's colour does two jobs. Member colour says **who**: each person in the family has one hue,
graded from a pale quiet-day chip to a deep full-day chip. Everything else (canvas, cards, text,
buttons) is warm neutral ink and white, so member colour never has to compete with chrome. Status
colours (danger, success, warning, info) say **what state** and appear only in messages and forms.

Every value is generated from OKLCH, except the three warm canvas colours chosen on 2026-10-27
(`#f2ece1`, `#f8f4ec`, `#e6ded0`), which are kept as chosen. All of them are measured. Nothing ships
unless `docs/design/check_colours.py` passes.

## Where it lives

| File | What it is |
|---|---|
| `events/static/events/shell.css` | The tokens, between `/* Colour tokens.` and `/* End of colour tokens. */`. Generated; do not hand-edit values. |
| `docs/design/palette.py` | The source: step lightness, deck chroma, member hues, neutrals and status colours in OKLCH. |
| `docs/design/check_colours.py` | The gate. Exits 1 if any pair, ramp or colour-blind check fails. |
| `docs/design/colourmath.py`, `cssread.py` | Standard-library helpers (OKLCH, CIEDE2000, WCAG, APCA, Machado CVD simulation, a small CSS reader). |

```sh
python docs/design/check_colours.py            # the gate (CI-friendly, exit 1 on failure)
python docs/design/check_colours.py --verbose  # every passing check too
python docs/design/check_colours.py --audit    # every hard-coded colour left, with its nearest role
python docs/design/palette.py --write          # regenerate the token region after editing palette.py
python docs/design/palette.py --ramp 200       # a member ramp for OKLCH hue 200
```

No packages are needed beyond Python 3.9+.

## Rules

1. **Colour is data.** A member hue means that person and nothing else. Do not use deck colours
   (`--a1` to `--h5`) for decoration or chrome. Use status colours only for state.
2. **Depth means load, for everyone.** Every member shares the same lightness at each step
   (OKLCH L .955 .875 .775 .545 .435), so a Wednesday chip at step 3 reads equally full for every member.
   Steps 1-3 carry `--paint-ink`, steps 4-5 carry `--on-paint-deep` (white).
3. **Colour is never the only cue.** Names and times are printed on every chip and card, and a dashed edge
   marks "not confirmed". The colour-blind checks are a quality bar, not the only safeguard.
4. **One warm neutral ramp.** Every grey sits at the canvas hue (OKLCH h 83). Older cool greys
   (green-grey, h 155-165) are gone from the tokens.
5. **Dark mode remaps roles, not paint.** Surfaces, text and status colours change. Chip paint stays the
   same, so a member's colour is identical day and night. Only step 1 (quiet days) dims into the dark card.
6. **Components use roles.** Write `var(--muted)`, not `var(--n47)` and never a hex value.

## Roles

Global roles are declared on `:root`, so stylesheets for pages not yet ported (accounts.css,
checklists.css) can use them. The four names the older pages also define on `:root` (`--ink`,
`--muted`, `--line`, `--track`) stay on `.ui, body.paint`, as before, and so does the canvas
`--counter`; ported pages also set `--counter` on the root (`:root:has(body.paint)`) so the overscroll
area matches the theme. Dark values apply to ported pages only (`body.paint`): from the theme switch
(`data-theme`, set by theme_boot.js) or, with scripts off, from `prefers-color-scheme`.

| Role | Light | Dark | Scope | Use for |
|---|---|---|---|---|
| `--counter` | `--n94` | `--n18` | `.ui`, `body.paint` | The canvas (page background). |
| `--card` | `--n100` | `--n23` | global | Cards, the top bar, inputs. Also the text colour on a filled status colour. |
| `--raised` | `--n97` | `--n27` | global | Hover rows, menus, pills on a card. |
| `--line` | `--n90` | `--n31` | `.ui`, `body.paint` | Dividers and decorative borders. Not for control edges. |
| `--control` | `--n56` | `--n56` | global | Edges that identify a control: inputs, quiet buttons, checkboxes. At least 3:1 on every surface. |
| `--ink` | `--n20` | `--n97` | `.ui`, `body.paint` | Text and icons. |
| `--muted` | `--n47` | `--n80` | `.ui`, `body.paint` | Secondary text. At least 4.5:1 and APCA Lc 60 on every surface. |
| `--primary`, `--on-primary` | `--n20`, `--n100` | `--n97`, `--n18` | global | The one primary action per view. |
| `--accent` | `--n20` | `--n97` | `body.paint` | `accent-color` of native checkboxes (drivers.css). |
| `--paint-ink` | `--n20` | `--n20` | global | Text on chip steps 1-3. The same in both themes. |
| `--on-paint-deep` | `--n100` | `--n100` | global | Text on chip steps 4-5, ticks on step-4 boxes. White in both themes: the light ink (`--n97`) drops to 4.24:1 on the greenest step 4. (New; paint.css still writes `#fff`.) |
| `--focus` | `--n20` | `--n97` | global | Focus outlines. (New.) |
| `--selection`, `--on-selection` | `--n80`, `--n20` | `--n31`, `--n97` | global | `::selection`. (New.) |
| `--shadow` | `--n18` | `--n18` | global | Shadow colour, mixed per use: `color-mix(in srgb, var(--shadow) 12%, transparent)`. (New.) |
| `--backdrop` | `--n18` at 40% | black at 64% | global | Dialog and sheet backdrop, ready to use as a background. (New.) |
| `--fixed-white`, `--fixed-ink` | `--n100`, `--n20` | same | global | A panel that stays white in both themes, and text on it: QR codes, images that need paper. (New.) |
| `--danger`, `--success`, `--warning`, `--info` | see below | see below | global | Status text, edges and fills. (Three new.) |
| `--danger-soft`, `--success-soft`, `--warning-soft`, `--info-soft` | see below | see below | global | Status backgrounds behind `--ink` or the matching status text. (New.) |
| `--track`, `--track-edge`, `--glass-tint`, `--glass-glow` | | | `.ui`, `body.paint` | The liquid-glass nav and switch pills. |
| `--s1` to `--s5`, `--name` | | | `[data-hue]` | One member's ramp and colour name inside an element with `data-hue="a".."h"` (`content: var(--name)` prints "Harbor blue"). |
| `--a1` to `--h5`, `--a-name` to `--h-name` | | | deck | The deck itself. Components read `--s1..--s5` and `--name`, not these. |
| `--n100` to `--n18` | | | `:root` | The neutral ramp. Used only inside the token region. |

Text on a filled status colour uses `--card`. Every status colour clears 4.5:1 against the card, and
contrast is symmetric, so this holds in both themes.

A colour a parent picks for a member (`style="--hue:#rrggbb"`) is redrawn at the decks' step
lightness with relative colour syntax (`oklch(from var(--hue) .775 min(c, .113) h)`). The chroma
caps are the largest every hue can reach in sRGB at that lightness, so any pick keeps the chip
contrast the decks pass. The checker tries 432 picks in both themes. Browsers without relative colour
syntax show the member's deck colour instead.

## Values

### Neutral ramp

| Token | Hex | OKLCH (L C h) | Used by |
|---|---|---|---|
| `--n100` | `#ffffff` | 1.000 0.000 0 | light `--card`, light `--on-primary`, light `--on-paint-deep`, light `--fixed-white`, dark `--on-paint-deep`, dark `--fixed-white` |
| `--n97` | `#f8f4ec` | 0.968 0.011 85 | light `--raised`, dark `--ink`, dark `--primary`, dark `--accent`, dark `--focus`, dark `--on-selection` |
| `--n94` | `#f2ece1` | 0.945 0.016 83 | light `--counter` |
| `--n90` | `#e6ded0` | 0.903 0.021 82 | light `--line` |
| `--n80` | `#c2bdb5` | 0.800 0.012 80 | light `--selection`, dark `--muted` |
| `--n56` | `#78746c` | 0.560 0.013 85 | light `--control`, dark `--control` |
| `--n47` | `#5e5a53` | 0.469 0.012 82 | light `--muted` |
| `--n31` | `#33302b` | 0.311 0.010 81 | dark `--line`, dark `--selection` |
| `--n27` | `#292622` | 0.270 0.009 75 | dark `--raised` |
| `--n23` | `#1f1d19` | 0.231 0.008 85 | dark `--card` |
| `--n20` | `#191714` | 0.206 0.007 78 | light `--ink`, light `--primary`, light `--accent`, light `--paint-ink`, light `--fixed-ink`, light `--focus`, light `--on-selection`, dark `--paint-ink`, dark `--fixed-ink` |
| `--n18` | `#13110f` | 0.179 0.005 67 | light `--shadow`, dark `--counter`, dark `--on-primary`, dark `--shadow` |

### Member decks

Step lightness (OKLCH L): 1 = .955, 2 = .875, 3 = .775, 4 = .545, 5 = .435. "Dark 1" is step 1 in dark mode.

**Primaries** (chroma per step 0.032 0.075 0.13 0.155 0.13)

| Member | Name | Hue | Chroma x | 1 | 2 | 3 | 4 | 5 | Dark 1 |
|---|---|---|---|---|---|---|---|---|---|
| a | Harbor blue | 262 | 1.00 | `#e9f0fe` | `#c4d7f9` | `#92b6f8` | `#3c6bc9` | `#274d98` | `#232c3d` |
| b | Jade | 166 | 0.93 | `#def7ec` | `#aae5cc` | `#5dcea5` | `#158362` | `#0d6047` | `#213026` |
| c | Marigold | 93 | 1.39 | `#faf0cf` | `#edd584` | `#d5b334` | `#906811` | `#694b0a` | `#342a1a` |
| d | Coral | 17 | 0.93 | `#feebeb` | `#f9c8c8` | `#f9959a` | `#b44551` | `#872e38` | `#3d2422` |
| e | Teal | 197 | 0.80 | `#ddf6f6` | `#a8e3e3` | `#56cacc` | `#158081` | `#0d5d5e` | `#212f2c` |
| f | Violet | 300 | 0.95 | `#f2edfe` | `#dbcef9` | `#c1a4f8` | `#7e58b8` | `#5c3e8a` | `#302839` |
| g | Magenta | 347 | 0.90 | `#feeaf3` | `#f8c5de` | `#ec97c4` | `#a8497f` | `#7d325d` | `#3a252c` |
| h | Leaf | 125 | 0.86 | `#ecf4e0` | `#ccdeb0` | `#a5c372` | `#5e7d12` | `#445a0a` | `#2a2e1a` |

**Garden** (chroma per step 0.026 0.058 0.095 0.105 0.088)

| Member | Name | Hue | Chroma x | 1 | 2 | 3 | 4 | 5 | Dark 1 |
|---|---|---|---|---|---|---|---|---|---|
| a | Sage | 151 | 0.70 | `#e8f4ea` | `#c3dec9` | `#97c2a1` | `#4f7d5a` | `#365b40` | `#272f24` |
| b | Plum | 323 | 1.00 | `#f9ebfb` | `#e9caed` | `#d3a2da` | `#8d5a94` | `#683f6d` | `#342731` |
| c | Ochre | 80 | 1.20 | `#fceed9` | `#efd1a2` | `#dcad5b` | `#956511` | `#6d490a` | `#352a1a` |
| d | Teal | 194 | 0.81 | `#e1f5f4` | `#b3e0df` | `#78c6c4` | `#1e7f7e` | `#0d5d5c` | `#212f2c` |
| e | Lavender | 283 | 1.00 | `#eeeffe` | `#d0d2f9` | `#adaef1` | `#6867ab` | `#4b4a80` | `#2b2b36` |
| f | Terracotta | 44 | 0.98 | `#feece5` | `#f7cbb8` | `#e8a385` | `#a15b3b` | `#784027` | `#39281e` |
| g | Dusty blue | 245 | 0.97 | `#e5f2fe` | `#b8dbf9` | `#82bced` | `#3576a7` | `#21557d` | `#232d35` |
| h | Moss | 109 | 1.00 | `#f1f2de` | `#d8daae` | `#babb72` | `#75751f` | `#55550d` | `#2e2d1a` |

**Bold** (chroma per step 0.04 0.1 0.16 0.19 0.155)

| Member | Name | Hue | Chroma x | 1 | 2 | 3 | 4 | 5 | Dark 1 |
|---|---|---|---|---|---|---|---|---|---|
| a | Cobalt | 270 | 1.00 | `#ebf0fe` | `#c8d5f9` | `#9cb3f8` | `#4662dd` | `#3146a5` | `#242b41` |
| b | Magenta | 349 | 0.75 | `#feeaf3` | `#f9c4dd` | `#ef95c2` | `#aa487e` | `#7d325b` | `#3a252c` |
| c | Emerald | 158 | 0.91 | `#ddf8e7` | `#a2e9c0` | `#53d192` | `#158555` | `#0d613c` | `#213023` |
| d | Tangerine | 56 | 0.97 | `#feece1` | `#f9cbac` | `#f89d56` | `#a65911` | `#7a400a` | `#39281a` |
| e | Violet | 302 | 0.89 | `#f3edfe` | `#ddcdf9` | `#c4a2f8` | `#8351c0` | `#60398f` | `#31273a` |
| f | Red | 24 | 0.87 | `#feebea` | `#f9c8c4` | `#f99790` | `#bd3c3d` | `#8c292a` | `#3f231f` |
| g | Cyan | 207 | 0.94 | `#d4f8fd` | `#86e8f5` | `#3bcbdc` | `#157e8a` | `#0c5c65` | `#212f2e` |
| h | Gold | 100 | 1.18 | `#f7f2ce` | `#e8d878` | `#ccb734` | `#8e6911` | `#684c0a` | `#342b1a` |

### Status colours

| Role | Light | Dark |
|---|---|---|
| `--danger` | `#b3261e` | `#ffa098` |
| `--danger-soft` | `#ffeeeb` | `#3c201c` |
| `--success` | `#297243` | `#82d399` |
| `--success-soft` | `#e9f7ec` | `#192f1f` |
| `--warning` | `#8d5b11` | `#ebc070` |
| `--warning-soft` | `#fef2d5` | `#352812` |
| `--info` | `#3063a6` | `#95c1fb` |
| `--info-soft` | `#ebf3ff` | `#1c2a3c` |

## Measurements

All numbers below are printed by `python docs/design/check_colours.py --markdown`; rerun it rather than
editing them. Thresholds: running text 4.5:1 and APCA Lc 75; small or secondary text (chips, labels)
4.5:1 and Lc 60; large text 3:1 and Lc 45; control edges, icons and focus rings 3:1 (WCAG 1.4.11).
APCA is APCA-W3 0.0.98G-4g; Lc is negative for light text on dark.

### Text and edges on ported pages

| Pair | Where | Theme | Text / edge | Background | WCAG | APCA Lc | Needs |
|---|---|---|---|---|---|---|---|
| Page text | `body.paint, h1, .empty` | light | `#191714` | `#f2ece1` | 15.21 | 94 | 4.5:1, Lc 75 |
| Card text | `.card .id b, .rail, .auth-form` | light | `#191714` | `#ffffff` | 17.89 | 105 | 4.5:1, Lc 75 |
| Text on raised (hover rows, menus) | `.bar .nav a:hover, .acct-menu a:hover` | light | `#191714` | `#f8f4ec` | 16.31 | 98 | 4.5:1, Lc 75 |
| Muted on canvas | `.span, .days, .legend` | light | `#5e5a53` | `#f2ece1` | 5.83 | 73 | 4.5:1, Lc 60 |
| Muted on card | `.id small, .rail .sub, .row small, .note, .lede, helptext, placeholder` | light | `#5e5a53` | `#ffffff` | 6.86 | 84 | 4.5:1, Lc 60 |
| Muted on raised | `.palette-tab summary (open), menus` | light | `#5e5a53` | `#f8f4ec` | 6.25 | 77 | 4.5:1, Lc 60 |
| Nav links on the bar track | `.bar .nav a` | light | `#5e5a53` | `#ebe5d9` | 5.45 | 68 | 4.5:1, Lc 60 |
| Week switcher links on the canvas | `.seg a` | light | `#5e5a53` | `#e8e1d3` | 5.27 | 66 | 4.5:1, Lc 60 |
| Current week switcher pill | `.seg [aria-current]` | light | `#191714` | `#f0ebe3` | 15.11 | 93 | 4.5:1, Lc 60 |
| Primary button | `.btn, .skip` | light | `#ffffff` | `#191714` | 17.89 | -107 | 4.5:1, Lc 60 |
| Primary button, hover (88% opacity) | `.btn:hover` | light | `#fdfdfb` | `#33312d` | 12.80 | -101 | 4.5:1, Lc 60 |
| Control edge on canvas | `.btn-quiet on .head` | light | `#78746c` | `#f2ece1` | 3.96 | 61 | 3.0:1 |
| Control edge on card | `inputs, selects, .btn-quiet` | light | `#78746c` | `#ffffff` | 4.65 | 72 | 3.0:1 |
| Control edge on raised | `.btn-quiet:hover` | light | `#78746c` | `#f8f4ec` | 4.24 | 66 | 3.0:1 |
| Focus ring on canvas | `:focus-visible` | light | `#191714` | `#f2ece1` | 15.21 | 94 | 3.0:1 |
| Focus ring on card | `:focus-visible` | light | `#191714` | `#ffffff` | 17.89 | 105 | 3.0:1 |
| Switch icons on the track | `.bar .switch .track` | light | `#5e5a53` | `#ebe5d9` | 5.45 | 68 | 3.0:1 |
| Checkbox accent on card | `drivers.css accent-color` | light | `#191714` | `#ffffff` | 17.89 | 105 | 3.0:1 |
| Error text on card | `.errorlist` | light | `#b3261e` | `#ffffff` | 6.54 | 81 | 4.5:1, Lc 60 |
| Error text on canvas | `.errorlist outside cards` | light | `#b3261e` | `#f2ece1` | 5.56 | 70 | 4.5:1, Lc 60 |
| Error flash | `.flash-error` | light | `#b3261e` | `#fbeeec` | 5.79 | 73 | 4.5:1, Lc 60 |
| Form error summary | `.errorlist.nonfield` | light | `#b3261e` | `#faeae8` | 5.62 | 71 | 4.5:1, Lc 60 |
| Invalid input edge | `input[aria-invalid]` | light | `#b3261e` | `#ffffff` | 6.54 | 81 | 3.0:1 |
| Danger on its tint | `--danger on --danger-soft` | light | `#b3261e` | `#ffeeeb` | 5.81 | 73 | 4.5:1, Lc 60 |
| Success on its tint | `--success on --success-soft` | light | `#297243` | `#e9f7ec` | 5.30 | 72 | 4.5:1, Lc 60 |
| Warning on its tint | `--warning on --warning-soft` | light | `#8d5b11` | `#fef2d5` | 5.19 | 71 | 4.5:1, Lc 60 |
| Info on its tint | `--info on --info-soft` | light | `#3063a6` | `#ebf3ff` | 5.43 | 72 | 4.5:1, Lc 60 |
| Success on card | `--success on --card` | light | `#297243` | `#ffffff` | 5.86 | 79 | 4.5:1, Lc 60 |
| Warning on card | `--warning on --card` | light | `#8d5b11` | `#ffffff` | 5.77 | 79 | 4.5:1, Lc 60 |
| Info on card | `--info on --card` | light | `#3063a6` | `#ffffff` | 6.07 | 80 | 4.5:1, Lc 60 |
| Text on a filled status colour | `--card on --success/--danger` | light | `#ffffff` | `#297243` | 5.86 | -84 | 4.5:1, Lc 60 |
| Text on filled danger | `--card on --danger` | light | `#ffffff` | `#b3261e` | 6.54 | -86 | 4.5:1, Lc 60 |
| Text on filled warning | `--card on --warning` | light | `#ffffff` | `#8d5b11` | 5.77 | -84 | 4.5:1, Lc 60 |
| Text on filled info | `--card on --info` | light | `#ffffff` | `#3063a6` | 6.07 | -85 | 4.5:1, Lc 60 |
| Ink on its status tints | `flash text on --*-soft` | light | `#191714` | `#fef2d5` | 16.08 | 97 | 4.5:1, Lc 75 |
| Selection | `::selection (--selection)` | light | `#191714` | `#c2bdb5` | 9.57 | 67 | 4.5:1, Lc 60 |
| Fixed light panel (QR code) | `--fixed-ink on --fixed-white` | light | `#191714` | `#ffffff` | 17.89 | 105 | 4.5:1, Lc 75 |
| Fixed light panel edge on canvas | `--fixed-white vs --counter` | light | `#ffffff` | `#f2ece1` | 1.18 | -10 | 1.0:1 |
| Clock switch label (fixed light look) | `.bar .clock-switch .lbl` | light | `#4b504d` | `#e3e7e4` | 6.59 | 74 | 4.5:1, Lc 60 |
| Clock switch current label | `.bar .clock-switch .lbl` | light | `#181a19` | `#f6f7f6` | 16.29 | 99 | 4.5:1, Lc 60 |
| Page text | `body.paint, h1, .empty` | dark | `#f8f4ec` | `#13110f` | 17.17 | -100 | 4.5:1, Lc 75 |
| Card text | `.card .id b, .rail, .auth-form` | dark | `#f8f4ec` | `#1f1d19` | 15.34 | -99 | 4.5:1, Lc 75 |
| Text on raised (hover rows, menus) | `.bar .nav a:hover, .acct-menu a:hover` | dark | `#f8f4ec` | `#292622` | 13.73 | -98 | 4.5:1, Lc 75 |
| Muted on canvas | `.span, .days, .legend` | dark | `#c2bdb5` | `#13110f` | 10.08 | -67 | 4.5:1, Lc 60 |
| Muted on card | `.id small, .rail .sub, .row small, .note, .lede, helptext, placeholder` | dark | `#c2bdb5` | `#1f1d19` | 9.01 | -65 | 4.5:1, Lc 60 |
| Muted on raised | `.palette-tab summary (open), menus` | dark | `#c2bdb5` | `#292622` | 8.06 | -64 | 4.5:1, Lc 60 |
| Nav links on the bar track | `.bar .nav a` | dark | `#c2bdb5` | `#272420` | 8.25 | -64 | 4.5:1, Lc 60 |
| Week switcher links on the canvas | `.seg a` | dark | `#c2bdb5` | `#25221e` | 8.50 | -65 | 4.5:1, Lc 60 |
| Current week switcher pill | `.seg [aria-current]` | dark | `#f8f4ec` | `#3f3d3a` | 9.90 | -92 | 4.5:1, Lc 60 |
| Primary button | `.btn, .skip` | dark | `#13110f` | `#f8f4ec` | 17.17 | 99 | 4.5:1, Lc 60 |
| Primary button, hover (88% opacity) | `.btn:hover` | dark | `#13110f` | `#ddd9d1` | 13.35 | 83 | 4.5:1, Lc 60 |
| Control edge on canvas | `.btn-quiet on .head` | dark | `#78746c` | `#13110f` | 4.05 | -29 | 3.0:1 |
| Control edge on card | `inputs, selects, .btn-quiet` | dark | `#78746c` | `#1f1d19` | 3.62 | -28 | 3.0:1 |
| Control edge on raised | `.btn-quiet:hover` | dark | `#78746c` | `#292622` | 3.24 | -26 | 3.0:1 |
| Focus ring on canvas | `:focus-visible` | dark | `#f8f4ec` | `#13110f` | 17.17 | -100 | 3.0:1 |
| Focus ring on card | `:focus-visible` | dark | `#f8f4ec` | `#1f1d19` | 15.34 | -99 | 3.0:1 |
| Switch icons on the track | `.bar .switch .track` | dark | `#c2bdb5` | `#272420` | 8.25 | -64 | 3.0:1 |
| Checkbox accent on card | `drivers.css accent-color` | dark | `#f8f4ec` | `#1f1d19` | 15.34 | -99 | 3.0:1 |
| Error text on card | `.errorlist` | dark | `#ffa098` | `#1f1d19` | 8.60 | -63 | 4.5:1, Lc 60 |
| Error text on canvas | `.errorlist outside cards` | dark | `#ffa098` | `#13110f` | 9.63 | -64 | 4.5:1, Lc 60 |
| Error flash | `.flash-error` | dark | `#ffa098` | `#2e2622` | 7.56 | -62 | 4.5:1, Lc 60 |
| Form error summary | `.errorlist.nonfield` | dark | `#ffa098` | `#322924` | 7.29 | -61 | 4.5:1, Lc 60 |
| Invalid input edge | `input[aria-invalid]` | dark | `#ffa098` | `#1f1d19` | 8.60 | -63 | 3.0:1 |
| Danger on its tint | `--danger on --danger-soft` | dark | `#ffa098` | `#3c201c` | 7.58 | -62 | 4.5:1, Lc 60 |
| Success on its tint | `--success on --success-soft` | dark | `#82d399` | `#192f1f` | 7.99 | -66 | 4.5:1, Lc 60 |
| Warning on its tint | `--warning on --warning-soft` | dark | `#ebc070` | `#352812` | 8.41 | -68 | 4.5:1, Lc 60 |
| Info on its tint | `--info on --info-soft` | dark | `#95c1fb` | `#1c2a3c` | 7.81 | -64 | 4.5:1, Lc 60 |
| Success on card | `--success on --card` | dark | `#82d399` | `#1f1d19` | 9.40 | -68 | 4.5:1, Lc 60 |
| Warning on card | `--warning on --card` | dark | `#ebc070` | `#1f1d19` | 9.86 | -71 | 4.5:1, Lc 60 |
| Info on card | `--info on --card` | dark | `#95c1fb` | `#1f1d19` | 9.05 | -66 | 4.5:1, Lc 60 |
| Text on a filled status colour | `--card on --success/--danger` | dark | `#1f1d19` | `#82d399` | 9.40 | 69 | 4.5:1, Lc 60 |
| Text on filled danger | `--card on --danger` | dark | `#1f1d19` | `#ffa098` | 8.60 | 64 | 4.5:1, Lc 60 |
| Text on filled warning | `--card on --warning` | dark | `#1f1d19` | `#ebc070` | 9.86 | 71 | 4.5:1, Lc 60 |
| Text on filled info | `--card on --info` | dark | `#1f1d19` | `#95c1fb` | 9.05 | 67 | 4.5:1, Lc 60 |
| Ink on its status tints | `flash text on --*-soft` | dark | `#f8f4ec` | `#352812` | 13.08 | -97 | 4.5:1, Lc 75 |
| Selection | `::selection (--selection)` | dark | `#f8f4ec` | `#33302b` | 11.98 | -96 | 4.5:1, Lc 60 |
| Fixed light panel (QR code) | `--fixed-ink on --fixed-white` | dark | `#191714` | `#ffffff` | 17.89 | 105 | 4.5:1, Lc 75 |
| Fixed light panel edge on canvas | `--fixed-white vs --counter` | dark | `#ffffff` | `#13110f` | 18.84 | -107 | 1.0:1 |
| Clock switch label (fixed light look) | `.bar .clock-switch .lbl` | dark | `#4b504d` | `#e3e7e4` | 6.59 | 74 | 4.5:1, Lc 60 |
| Clock switch current label | `.bar .clock-switch .lbl` | dark | `#181a19` | `#f6f7f6` | 16.29 | 99 | 4.5:1, Lc 60 |

### Member chips

The worst member across all three decks, for each chip level.

| Chip | Theme | Worst member | Text | Background | WCAG | APCA Lc | Needs |
|---|---|---|---|---|---|---|---|
| l0 quiet chip | light | bold c | `#0d613c` | `#ddf8e7` | 6.68 | 78 | 4.5:1, Lc 60 |
| l1 chip | light | bold b | `#191714` | `#f9c4dd` | 11.90 | 79 | 4.5:1, Lc 60 |
| l2 chip, today rail event | light | bold b | `#191714` | `#ef95c2` | 8.30 | 60 | 4.5:1, Lc 60 |
| l3 chip, avatar dot | light | bold c | `#ffffff` | `#158555` | 4.65 | -77 | 4.5:1, Lc 60 |
| l4 chip | light | bold c | `#ffffff` | `#0d613c` | 7.52 | -91 | 4.5:1, Lc 60 |
| --on-paint-deep on step 4 | light | bold c | `#ffffff` | `#158555` | 4.65 | -77 | 4.5:1, Lc 60 |
| --on-paint-deep on step 5 | light | bold c | `#ffffff` | `#0d613c` | 7.52 | -91 | 4.5:1, Lc 60 |
| Step 4 edge on card | light | bold c | `#158555` | `#ffffff` | 4.65 | 72 | 3.0:1 |
| Selected text (paint.css) | light | bold a | `#111111` | `#c8d5f9` | 12.90 | 81 | 4.5:1, Lc 60 |
| l0 quiet chip (dark) | dark | bold b | `#f9c4dd` | `#3a252c` | 9.45 | -76 | 4.5:1, Lc 60 |
| l1 chip | dark | bold b | `#191714` | `#f9c4dd` | 11.90 | 79 | 4.5:1, Lc 60 |
| l2 chip, today rail event | dark | bold b | `#191714` | `#ef95c2` | 8.30 | 60 | 4.5:1, Lc 60 |
| l3 chip, avatar dot | dark | bold c | `#ffffff` | `#158555` | 4.65 | -77 | 4.5:1, Lc 60 |
| l4 chip | dark | bold c | `#ffffff` | `#0d613c` | 7.52 | -91 | 4.5:1, Lc 60 |
| --on-paint-deep on step 4 | dark | bold c | `#ffffff` | `#158555` | 4.65 | -77 | 4.5:1, Lc 60 |
| --on-paint-deep on step 5 | dark | bold c | `#ffffff` | `#0d613c` | 7.52 | -91 | 4.5:1, Lc 60 |
| Step 4 edge on card | dark | bold f | `#bd3c3d` | `#1f1d19` | 3.11 | -24 | 3.0:1 |
| Selected text (paint.css) | dark | bold a | `#111111` | `#c8d5f9` | 12.90 | 81 | 4.5:1, Lc 60 |

### Pages not yet ported

accounts.css and checklists.css, measured with the older pages' `--ink`, `--muted`, `--line` and
backgrounds (left) and as they will be once ported to `body.paint` (right).

| Pair | Where | Unported WCAG / Lc | Ported WCAG / Lc | Needs |
|---|---|---|---|---|
| Flash text | `accounts.css .flash` | 12.91 / 99 | 17.89 / 105 | 4.5:1, Lc 75 |
| Success flash | `accounts.css .flash-success` | 11.67 / 92 | 16.17 / 98 | 4.5:1, Lc 75 |
| Warning flash | `accounts.css .flash-warning` | 11.61 / 92 | 16.08 / 97 | 4.5:1, Lc 75 |
| Error flash | `accounts.css .flash-error` | 11.49 / 91 | 15.91 / 97 | 4.5:1, Lc 75 |
| Link button | `accounts.css .link-button` | 11.68 / 92 | 15.21 / 94 | 4.5:1, Lc 60 |
| Role option edge | `accounts.css .role-option` | 4.58 / 71 | 4.65 / 72 | 3.0:1 |
| Role option, checked | `accounts.css .role-option:checked` | 16.31 / 98 | 16.31 / 98 | 3.0:1 |
| Role option text, checked | `accounts.css .role-option:checked` | 11.77 / 93 | 16.31 / 98 | 4.5:1, Lc 75 |
| Member secondary line | `accounts.css .member-name small` | 5.07 / 74 | 6.86 / 84 | 4.5:1, Lc 60 |
| Kid avatar | `accounts.css .avatar-kid` | 11.77 / 93 | 16.31 / 98 | 4.5:1, Lc 60 |
| Pending badge | `accounts.css .status-pending` | 5.19 / 71 | 5.19 / 71 | 4.5:1, Lc 60 |
| Active badge | `accounts.css .status-active` | 5.30 / 72 | 5.30 / 72 | 4.5:1, Lc 60 |
| Kid page text | `accounts.css .kid-dashboard` | 11.68 / 92 | 15.21 / 94 | 4.5:1, Lc 75 |
| Kid hero | `accounts.css .kid-hero` | 12.91 / 99 | 17.89 / 105 | 4.5:1, Lc 75 |
| Kid page links | `accounts.css .kid-dashboard a` | 12.91 / 99 | 17.89 / 105 | 4.5:1, Lc 75 |
| Kid today tile | `accounts.css .kid-today` | 12.91 / 99 | 17.89 / 105 | 4.5:1, Lc 75 |
| Kid event card | `accounts.css .kid-event` | 12.91 / 99 | 17.89 / 105 | 4.5:1, Lc 75 |
| Kid empty note | `accounts.css .kid-empty` | 11.77 / 93 | 16.31 / 98 | 4.5:1, Lc 75 |
| Kid family bubble | `accounts.css .kid-bubble` | 11.77 / 93 | 16.31 / 98 | 3.0:1, Lc 45 |
| Backup code | `accounts.css .backup-codes code` | 12.71 / 98 | 17.89 / 105 | 4.5:1, Lc 75 |
| Checklist primary button | `checklists.css .cl-button` | 17.89 / -107 | 17.89 / -107 | 4.5:1, Lc 60 |
| Checklist primary button, hover | `checklists.css .cl-button:hover` | 13.59 / -103 | 13.59 / -103 | 4.5:1, Lc 60 |
| Checklist danger button, hover | `checklists.css .cl-button-danger:hover` | 5.28 / 67 | 5.28 / 67 | 4.5:1, Lc 60 |
| Done task toggle, hover | `checklists.css .cl-task-toggle:hover` | 5.86 / 79 | 5.86 / 79 | 4.5:1, Lc 60 |
| Checklist quiet button | `checklists.css .cl-button-secondary` | 12.91 / 99 | 17.89 / 105 | 4.5:1, Lc 60 |
| Checklist quiet button edge | `checklists.css .cl-button-secondary` | 4.65 / 72 | 4.65 / 72 | 3.0:1 |
| Checklist danger button | `checklists.css .cl-button-danger` | 5.81 / 73 | 5.81 / 73 | 4.5:1, Lc 60 |
| Checklist card text | `checklists.css .cl-card` | 12.91 / 99 | 17.89 / 105 | 4.5:1, Lc 75 |
| Checklist secondary text | `checklists.css .cl-muted, .cl-subtitle` | 5.15 / 75 | 6.86 / 84 | 4.5:1, Lc 60 |
| Checklist secondary on page | `checklists.css .cl-subtitle` | 4.65 / 69 | 5.83 / 73 | 4.5:1, Lc 60 |
| Checklist event tag | `checklists.css .cl-event` | 11.77 / 93 | 16.31 / 98 | 4.5:1, Lc 60 |
| Task count badge | `checklists.css .cl-task-count` | 5.15 / 75 | 6.86 / 84 | 4.5:1, Lc 60 |
| Checklist links | `checklists.css .checklist-page a, .cl-back` | 11.68 / 92 | 15.21 / 94 | 4.5:1, Lc 75 |
| Add-task field edge | `checklists.css .cl-add-field input` | 4.65 / 72 | 4.65 / 72 | 3.0:1 |
| Progress bar fill | `checklists.css .cl-progress` | 4.36 / 60 | 4.39 / 60 | 3.0:1 |
| Progress card number | `checklists.css .cl-progress-number` | 5.30 / 72 | 5.30 / 72 | 3.0:1, Lc 45 |
| Progress card text | `checklists.css .cl-progress-card` | 11.67 / 92 | 16.17 / 98 | 4.5:1, Lc 75 |
| Done task text | `checklists.css .is-complete .cl-task-text` | 4.65 / 69 | 6.20 / 77 | 4.5:1, Lc 60 |
| Done by line | `checklists.css .cl-completed-by` | 4.65 / 69 | 6.20 / 77 | 4.5:1, Lc 60 |
| Checkbox edge | `checklists.css .cl-check-icon` | 4.65 / 72 | 4.65 / 72 | 3.0:1 |
| Checked box tick | `checklists.css .is-complete .cl-check-icon` | 5.86 / -84 | 5.86 / -84 | 4.5:1, Lc 60 |
| Checked box on its row | `checklists.css .is-complete .cl-check-icon` | 5.30 / 72 | 5.30 / 72 | 3.0:1 |
| Task action links | `checklists.css .cl-task-actions a` | 12.91 / 99 | 17.89 / 105 | 4.5:1, Lc 60 |
| Optional tag | `checklists.css .cl-optional` | 4.69 / 69 | 6.25 / 77 | 4.5:1, Lc 60 |
| Form field edge | `checklists.css .cl-field input` | 4.65 / 72 | 4.65 / 72 | 3.0:1 |
| Form focus ring | `checklists.css .cl-field input:focus` | 17.89 / 105 | 17.89 / 105 | 3.0:1 |

### Colour-blind separation

CIEDE2000 between members at steps 3 and 4, simulated with Machado, Oliveira and Fernandes (2009) at full
severity. Gates: neighbours (a-b ... h-a) at least 10 in every vision type; the first four members
(a-d, most families) at least 6; all eight at least 12 for normal vision. Dark mode uses the same steps
3 and 4, so its rows are identical and omitted.

| Deck | Theme | Step | Vision | Neighbours (min, pair) | All 8 members | First 4 members |
|---|---|---|---|---|---|---|
| primaries | light | 3 | normal | 16.9 (f-g) | 13.2 (d-g) | 32.9 (b-c) |
| primaries | light | 3 | protan | 10.7 (f-g) | 2.8 (a-f) | 10.3 (b-d) |
| primaries | light | 3 | deutan | 10.6 (e-f) | 0.4 (a-f) | 6.6 (b-d) |
| primaries | light | 3 | tritan | 10.3 (a-b) | 2.3 (b-e) | 10.3 (a-b) |
| primaries | light | 4 | normal | 17.4 (f-g) | 13.0 (b-e) | 33.7 (c-d) |
| primaries | light | 4 | protan | 11.3 (f-g) | 4.1 (a-f) | 12.3 (b-d) |
| primaries | light | 4 | deutan | 10.6 (c-d) | 1.1 (c-h) | 9.2 (b-d) |
| primaries | light | 4 | tritan | 10.4 (a-b) | 2.2 (b-e) | 10.4 (a-b) |
| garden | light | 3 | normal | 15.4 (h-a) | 13.1 (a-d) | 13.1 (a-d) |
| garden | light | 3 | protan | 11.0 (h-a) | 3.0 (e-g) | 14.2 (a-c) |
| garden | light | 3 | deutan | 12.5 (h-a) | 0.6 (e-g) | 6.5 (b-d) |
| garden | light | 3 | tritan | 10.5 (b-c) | 3.7 (d-g) | 8.7 (a-d) |
| garden | light | 4 | normal | 17.9 (h-a) | 13.3 (b-e) | 14.8 (a-d) |
| garden | light | 4 | protan | 13.0 (h-a) | 3.2 (c-h) | 14.1 (a-c) |
| garden | light | 4 | deutan | 12.9 (d-e) | 0.8 (e-g) | 6.8 (b-d) |
| garden | light | 4 | tritan | 10.7 (b-c) | 4.0 (c-f) | 6.5 (a-d) |
| bold | light | 3 | normal | 27.8 (a-b) | 13.0 (a-e) | 27.8 (a-b) |
| bold | light | 3 | protan | 10.4 (c-d) | 2.7 (a-e) | 10.4 (c-d) |
| bold | light | 3 | deutan | 13.7 (c-d) | 0.2 (a-e) | 13.7 (c-d) |
| bold | light | 3 | tritan | 21.8 (e-f) | 2.7 (d-f) | 6.1 (b-d) |
| bold | light | 4 | normal | 28.2 (a-b) | 13.0 (d-h) | 28.2 (a-b) |
| bold | light | 4 | protan | 13.4 (c-d) | 2.8 (d-h) | 13.4 (c-d) |
| bold | light | 4 | deutan | 14.8 (c-d) | 0.8 (d-h) | 14.8 (c-d) |
| bold | light | 4 | tritan | 23.5 (d-e) | 3.7 (c-g) | 6.4 (b-d) |

### Colours a parent picks

432 picks (every 5 degrees of hue, six lightness and chroma levels, clipped to sRGB) in both themes. The
worst case for each chip:

| Chip | Theme | WCAG | APCA Lc | Picked |
|---|---|---|---|---|
| --on-paint-deep on step 4 | dark | 4.71 | -78 | `#00f5b3` |
| --on-paint-deep on step 5 | dark | 7.60 | -91 | `#599576` |
| Step 4 edge on card | dark | 3.22 | -24 | `#5a0026` |
| l0 quiet chip (dark) | dark | 9.41 | -76 | `#b30079` |
| l1 chip | dark | 11.92 | 79 | `#b30079` |
| l2 chip, today rail event | dark | 8.31 | 60 | `#550436` |
| l3 chip, avatar dot | dark | 4.71 | -78 | `#00f5b3` |
| l4 chip | dark | 7.60 | -91 | `#599576` |
| --on-paint-deep on step 4 | light | 4.71 | -78 | `#00f5b3` |
| --on-paint-deep on step 5 | light | 7.60 | -91 | `#599576` |
| Step 4 edge on card | light | 4.71 | 72 | `#00f5b3` |
| l0 quiet chip | light | 6.71 | 78 | `#62946c` |
| l1 chip | light | 11.92 | 79 | `#b30079` |
| l2 chip, today rail event | light | 8.31 | 60 | `#550436` |
| l3 chip, avatar dot | light | 4.71 | -78 | `#00f5b3` |
| l4 chip | light | 7.60 | -91 | `#599576` |

## Before and after

Measured on `design/warm-canvas-background` (before) and this branch (after) with the same script.

| | Before | After |
|---|---|---|
| Gate (`check_colours.py`) | 275 failures (85 of them roles that did not exist yet) | 765 checks, 0 failures |
| Neutral hue (ink, muted, control, all dark surfaces) | OKLCH h 155-165, green-grey, against a canvas at h 83 | h 83 throughout |
| `--control` (input and quiet-button edges) | 1.67:1 on card, 1.42:1 on canvas; dark 2.04:1 | 4.65:1, 3.96:1; dark 3.62:1 |
| Lightness spread inside one step (step 3) | primaries 0.099, garden 0.054, bold 0.125 (OKLCH L) | 0.002 in every deck and step |
| Ink on step-3 chips (APCA) | as low as Lc 48 (bold) | at least Lc 60 |
| Dark secondary text on surfaces (APCA) | Lc 58.8 to 61.4 | Lc 64.0 to 66.7 |
| Dark step-4 edge on the card | 2.45:1 to 2.97:1 for 15 members | at least 3.11:1 |
| Neighbouring members, deuteranopia (CIEDE2000, steps 3-4) | primaries 1.4, garden 7.8, bold 1.9 | 10.6, 12.5, 13.7 |
| Neighbouring members, protanopia | 4.0, 6.9, 5.1 | 10.7, 11.0, 10.4 |
| Neighbouring members, tritanopia | 6.9, 9.3, 11.9 | 10.3, 10.5, 21.8 |
| First four members, any vision | 2.2, 3.1, 3.4 | 6.6, 6.5, 6.1 |
| Hard-coded colours in stylesheets (`--audit`) | 405 | 307 (accounts.css 38 to 0, checklists.css 60 to 0) |
| A parent-picked member colour (432 picks) | 268 picks fail a chip pair (ink on step 3 down to 3.69:1, white on step 4 to 4.06:1) | 0 fail |

What changed in the decks: each member keeps its colour family except where colour-blind separation
needed a move. Primaries: Fern became Jade (h 141 to 167) so it no longer matches Coral for
deuteranopes, Violet and Teal swapped slots (e, f), and Slate, a low-chroma blue-grey that could not be
told apart from anything, became Leaf. Garden: same families, hues adjusted. Bold: Lime became Emerald
(Lime and Tangerine were 3.4 apart for a deuteranope, and no hue inside either family reached 10 at a
shared lightness), and Red and Cyan swapped slots (f, g).

## Adding a member colour

1. Pick a hue: `python docs/design/palette.py --ramp 200` prints the five steps for OKLCH hue 200 in
   each deck's chroma. Yellows (h 78-100) automatically warm toward amber at steps 4-5.
2. Add it to the deck in `palette.py` (`DECKS`, as `(name, hue, chroma multiplier)`). A ninth member
   also needs `--i1..--i5` in the generator, `HUES` in both scripts, a `[data-hue="i"]` mapping and
   `DECK_HUES` in `events/timeline.py`.
3. `python docs/design/palette.py --write`, then `python docs/design/check_colours.py`.
4. If a colour-vision check fails, move the hue 5-10 degrees away from the neighbour the message names,
   or lower its chroma multiplier, and run again. Keep text on every chip.

To change a role, edit the `css_region()` block in `palette.py` (and `STATUS` or `NEUTRALS` for
values), rewrite the region, and add any new pair the pages use to `PAINT_PAIRS` or `LEGACY_PAIRS` in
the checker so it is measured from then on.

## Known limits

- **Twelve-pixel chip text.** Every chip pair meets WCAG 4.5:1 and APCA Lc 60. APCA's font table asks for
  Lc 90 or more at 12px/500, which no coloured chip reaches. Chip text at 13px/600 would add margin.
- **Eight members under full dichromacy.** Neighbours and the first four stay apart; some non-neighbours
  do not (for example Harbor blue and Violet for a deuteranope, 0.4). Names on chips and cards carry identity.
- **Dark-mode depth.** Chips keep their light-mode paint at night, so pale steps 1-2 are brighter than deep
  steps 4-5 on the dark page. Depth still reads as more pigment and the legend shows the ramp. A ramp that
  brightens with load in dark mode needs per-step text tokens in paint.css (below).
- **Pages not yet ported** keep their own `--ink`, `--muted` and `--line` until they move to `body.paint`.
  accounts.css and checklists.css are checked in both states.

## For the UI port: tokens to adopt

These are outside the colour region (paint.css, the chrome in shell.css, shell.js, event_form.css,
workspace.css), so this branch leaves them alone. `python docs/design/check_colours.py --audit` lists
every remaining literal with its nearest role.

| Where | Today | Use |
|---|---|---|
| paint.css `.l3`, `.l4`; shell.css `.dot` | `color: #fff` | `var(--on-paint-deep)` |
| shell.css `.dot` | `background: var(--s4, var(--ink))` with white text: white on near-white in dark mode when a user has no hue | always render `.me` with a `data-hue`, or style the no-hue case as `var(--primary)` on `var(--on-primary)` |
| paint.css `::selection` | `var(--a2)` (member a's colour) and `#111` | `var(--selection)`, `var(--on-selection)` |
| paint.css, shell.css `:focus-visible` | `var(--ink)` | `var(--focus)` |
| shell.css shadows | `#1a1c1b0d`, `rgb(0 0 0 / .14)` | `color-mix(in srgb, var(--shadow) 5%, transparent)` and so on; dialog backdrops `var(--backdrop)` |
| shell.css `.clock-switch` | cool greys `#e3e7e4`, `#d3d8d5`, `#f6f7f6`, `#4b504d`, `#181a19` | `--n90`, `--n80`, `--n97`, `--n47`, `--n20` (it is theme-invariant by design). Its 12h label is unreadable in dark mode on the base branch too. |
| shell.js `syncTheme` | theme-color `#1b1e1c` for dark | the dark card is now `#1f1d19`; read `getComputedStyle(document.body).getPropertyValue('--card')` |
| paint.css `[data-theme="dark"] .l0` | `color-mix(in oklab, var(--s5) 30%, var(--card))`, `color: var(--s2)` | dark `--a1..--h1` now hold exactly that mix, so `.l0 { background: var(--s1) }` works in both themes; keep `color: var(--s2)` in dark |
| paint.css `.flash` | only `.flash-error` is styled | `.flash-success`, `.flash-warning`, `.flash-info` from the `-soft` roles, as accounts.css does |
| paint.css on the UI branch (#127) | local `--on-deep` alias: `var(--card)` light, `var(--ink)` dark | `var(--on-paint-deep)`. With the new decks the dark alias gives 9 of 48 deep steps under 4.5:1 (worst 4.24:1, bold Emerald step 4); white passes all (4.65:1 and up). Its `.who-pick` no-hue fallback draws `--on-deep` on `--ink`, the same colour in dark mode. |
| two-step `.qr` on the UI branch | `color-scheme: light; background: Canvas` | `background: var(--fixed-white)` |
| family page colour pills | names left out | `content: var(--name)` inside `[data-hue]` |
| paint.css chip text | `.l1`/`.l2` read `--paint-ink`, `.l3`/`.l4` write `#fff`, dark `.l0` reads `--s2` | one text token per level, set next to `--s1..--s5`; the token region could then give dark mode a ramp that brightens with load |
| event_form.css, workspace.css, chat_home.html | 233 + 54 + 20 literals (terracotta, lavender, violet bubbles) | retire with the port; nearest roles in `--audit` |
