"""Colour maths for Syncmily's palette checks. Standard library only, so it runs in the
project venv.

sRGB <-> OKLab/OKLCH (Ottosson), CIELAB D65, WCAG 2.x contrast, APCA-W3 0.0.98G-4g Lc,
CIEDE2000, Machado et al. (2009) colour-vision-deficiency simulation at full severity,
and
OKLCH -> sRGB gamut mapping by chroma reduction. check_colours.py imports this module.
"""

import math

# --- sRGB transfer
# -------------------------------------------------------------------------


def _to_linear(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _from_linear(c):
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def hex_to_rgba(value):
    """'#rgb', '#rgba', '#rrggbb' or '#rrggbbaa' -> (r, g, b, a), each 0..1 (r, g, b
    gamma-encoded).
    """
    h = value.strip().lstrip("#")
    if len(h) in (3, 4):
        h = "".join(ch * 2 for ch in h)
    if len(h) not in (6, 8):
        raise ValueError(f"not a hex colour: {value!r}")
    r, g, b = (int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))
    a = int(h[6:8], 16) / 255 if len(h) == 8 else 1.0
    return (r, g, b, a)


def rgb_to_hex(rgb):
    r, g, b = (min(255, max(0, round(c * 255))) for c in rgb[:3])
    return f"#{r:02x}{g:02x}{b:02x}"


def over(fg, bg):
    """Composite an RGBA colour over an opaque one (in gamma-encoded sRGB, as browsers
    blend).
    """
    a = fg[3]
    return tuple(fg[i] * a + bg[i] * (1 - a) for i in range(3)) + (1.0,)


# --- OKLab / OKLCH
# -------------------------------------------------------------------------


def rgb_to_oklab(rgb):
    r, g, b = (_to_linear(c) for c in rgb[:3])
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b  # noqa: E741 (OKLab L)
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_, m_, s_ = (math.copysign(abs(v) ** (1 / 3), v) for v in (l, m, s))
    return (
        0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
        1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
        0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_,
    )


def oklab_to_linear(lab):
    L, a, b = lab
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b
    l, m, s = l_**3, m_**3, s_**3  # noqa: E741
    return (
        4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
        -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
        -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
    )


def oklab_to_rgb(lab):
    return tuple(_from_linear(c) for c in oklab_to_linear(lab))


def rgb_to_oklch(rgb):
    L, a, b = rgb_to_oklab(rgb)
    C = math.hypot(a, b)
    h = math.degrees(math.atan2(b, a)) % 360 if C > 1e-7 else 0.0
    return (L, C, h)


def oklch_to_lab(L, C, h):
    return (L, C * math.cos(math.radians(h)), C * math.sin(math.radians(h)))


def in_gamut(lin, eps=1e-6):
    return all(-eps <= c <= 1 + eps for c in lin)


def max_chroma(L, h, hi=0.4):
    """Largest OKLCH chroma at (L, h) that stays inside sRGB."""
    lo = 0.0
    for _ in range(40):
        mid = (lo + hi) / 2
        if in_gamut(oklab_to_linear(oklch_to_lab(L, mid, h))):
            lo = mid
        else:
            hi = mid
    return lo


def oklch_to_rgb(L, C, h):
    """OKLCH -> sRGB, reducing chroma (hue and lightness kept) until the colour fits the
    gamut.
    """
    C = (
        min(C, max_chroma(L, h))
        if not in_gamut(oklab_to_linear(oklch_to_lab(L, C, h)))
        else C
    )
    return tuple(min(1.0, max(0.0, c)) for c in oklab_to_rgb(oklch_to_lab(L, C, h)))


def oklch_to_rgb_clipped(L, C, h):
    """OKLCH -> sRGB by per-channel clipping: what a browser paints for an out-of-gamut
    colour.
    """
    return tuple(min(1.0, max(0.0, c)) for c in oklab_to_rgb(oklch_to_lab(L, C, h)))


def mix_oklab(a, b, p):
    """CSS color-mix(in oklab, a p, b): p is a's share, 0..1. Opaque colours only."""
    la, lb = rgb_to_oklab(a), rgb_to_oklab(b)
    return tuple(
        min(1.0, max(0.0, c))
        for c in oklab_to_rgb(tuple(x * p + y * (1 - p) for x, y in zip(la, lb)))
    ) + (1.0,)


# --- CIELAB (D65) and CIEDE2000
# -------------------------------------------------------------

_WHITE = (0.95047, 1.0, 1.08883)


def rgb_to_xyz(rgb):
    r, g, b = (_to_linear(c) for c in rgb[:3])
    return (
        0.4124564 * r + 0.3575761 * g + 0.1804375 * b,
        0.2126729 * r + 0.7151522 * g + 0.0721750 * b,
        0.0193339 * r + 0.1191920 * g + 0.9503041 * b,
    )


def _lab_f(t):
    d = 6 / 29
    return t ** (1 / 3) if t > d**3 else t / (3 * d * d) + 4 / 29


def linear_to_lab(lin):
    r, g, b = lin
    x = 0.4124564 * r + 0.3575761 * g + 0.1804375 * b
    y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
    z = 0.0193339 * r + 0.1191920 * g + 0.9503041 * b
    fx, fy, fz = (_lab_f(v / w) for v, w in zip((x, y, z), _WHITE))
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def rgb_to_lab(rgb):
    return linear_to_lab(tuple(_to_linear(c) for c in rgb[:3]))


def delta_e_2000(lab1, lab2):
    L1, a1, b1 = lab1
    L2, a2, b2 = lab2
    c1, c2 = math.hypot(a1, b1), math.hypot(a2, b2)
    cbar = (c1 + c2) / 2
    g = 0.5 * (1 - math.sqrt(cbar**7 / (cbar**7 + 25**7)))
    a1p, a2p = a1 * (1 + g), a2 * (1 + g)
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360 if c1p else 0.0
    h2p = math.degrees(math.atan2(b2, a2p)) % 360 if c2p else 0.0
    dLp, dCp = L2 - L1, c2p - c1p
    if c1p * c2p == 0:
        dhp = 0.0
    elif abs(h2p - h1p) <= 180:
        dhp = h2p - h1p
    elif h2p - h1p > 180:
        dhp = h2p - h1p - 360
    else:
        dhp = h2p - h1p + 360
    dHp = 2 * math.sqrt(c1p * c2p) * math.sin(math.radians(dhp / 2))
    Lbp, Cbp = (L1 + L2) / 2, (c1p + c2p) / 2
    if c1p * c2p == 0:
        hbp = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        hbp = (h1p + h2p) / 2
    elif h1p + h2p < 360:
        hbp = (h1p + h2p + 360) / 2
    else:
        hbp = (h1p + h2p - 360) / 2
    t = (
        1
        - 0.17 * math.cos(math.radians(hbp - 30))
        + 0.24 * math.cos(math.radians(2 * hbp))
        + 0.32 * math.cos(math.radians(3 * hbp + 6))
        - 0.20 * math.cos(math.radians(4 * hbp - 63))
    )
    d_theta = 30 * math.exp(-(((hbp - 275) / 25) ** 2))
    rc = 2 * math.sqrt(Cbp**7 / (Cbp**7 + 25**7))
    sl = 1 + 0.015 * (Lbp - 50) ** 2 / math.sqrt(20 + (Lbp - 50) ** 2)
    sc = 1 + 0.045 * Cbp
    sh = 1 + 0.015 * Cbp * t
    rt = -math.sin(math.radians(2 * d_theta)) * rc
    return math.sqrt(
        (dLp / sl) ** 2
        + (dCp / sc) ** 2
        + (dHp / sh) ** 2
        + rt * (dCp / sc) * (dHp / sh)
    )


# --- Contrast
# -------------------------------------------------------------------------------


def luminance(rgb):
    """WCAG 2.x relative luminance."""
    r, g, b = (_to_linear(c) for c in rgb[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def wcag_ratio(fg, bg):
    hi, lo = sorted((luminance(fg), luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def apca_lc(text, background):
    """APCA-W3 0.0.98G-4g lightness contrast. Positive: dark text on light; negative:
    light on dark.
    """

    def y(rgb):
        r, g, b = rgb[:3]
        return 0.2126729 * r**2.4 + 0.7151522 * g**2.4 + 0.0721750 * b**2.4

    def clamp(v):
        return v + (0.022 - v) ** 1.414 if v < 0.022 else v

    yt, yb = clamp(y(text)), clamp(y(background))
    if abs(yb - yt) < 0.0005:
        return 0.0
    if yb > yt:
        sapc = (yb**0.56 - yt**0.57) * 1.14
        out = 0.0 if sapc < 0.1 else sapc - 0.027
    else:
        sapc = (yb**0.65 - yt**0.62) * 1.14
        out = 0.0 if sapc > -0.1 else sapc + 0.027
    return out * 100


# --- Colour-vision deficiency (Machado, Oliveira & Fernandes 2009, severity 1.0)
# ------------

MACHADO = {
    "protan": (
        (0.152286, 1.052583, -0.204868),
        (0.114503, 0.786281, 0.099216),
        (-0.003882, -0.048116, 1.051998),
    ),
    "deutan": (
        (0.367322, 0.860646, -0.227968),
        (0.280085, 0.672501, 0.047413),
        (-0.011820, 0.042940, 0.968881),
    ),
    "tritan": (
        (1.255528, -0.076749, -0.178779),
        (-0.078411, 0.930809, 0.147602),
        (0.004733, 0.691367, 0.303900),
    ),
}


def simulate(rgb, kind):
    """The colour as seen with full protanopia, deuteranopia or tritanopia ('normal'
    passes through).
    """
    if kind == "normal":
        return tuple(rgb[:3])
    lin = [_to_linear(c) for c in rgb[:3]]
    m = MACHADO[kind]
    out = [sum(m[i][j] * lin[j] for j in range(3)) for i in range(3)]
    return tuple(_from_linear(min(1.0, max(0.0, c))) for c in out)


def cvd_delta_e(c1, c2, kind):
    return delta_e_2000(rgb_to_lab(simulate(c1, kind)), rgb_to_lab(simulate(c2, kind)))
