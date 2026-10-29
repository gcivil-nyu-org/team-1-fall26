"""Just enough CSS reading for the colour checks: rules, declarations, at-rule nesting,
colour literals.
"""

import re

COMMENT = re.compile(r"/\*.*?\*/", re.S)
HEX = re.compile(
    r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{4}|[0-9a-fA-F]{3})(?![0-9a-zA-Z_-])"
)
FUNC = re.compile(r"\b(?:rgba?|hsla?|oklch|oklab|lab|lch|hwb)\(\s*[^()]*\)")
NAMED = re.compile(r"(?<![\w-])(white|black)(?![\w-])", re.I)


class Rule:
    def __init__(self, selector, decls, at, line):
        self.selector = " ".join(selector.split())
        self.decls = decls  # list of (property, value, line)
        self.at = tuple(at)  # enclosing at-rule preludes, outermost first
        self.line = line

    def __repr__(self):
        return f"Rule({self.selector!r}, {len(self.decls)} decls, at={self.at})"


def _blank_comments(text):
    """Remove comments but keep line numbers stable."""
    return COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), text)


def _split_decls(body, first_line):
    """Split a declaration block on ';' outside parentheses and quotes."""
    out, depth, quote, start = [], 0, None, 0
    pieces = []
    for i, ch in enumerate(body):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == ";" and depth == 0:
            pieces.append((start, i))
            start = i + 1
    pieces.append((start, len(body)))
    for s, e in pieces:
        chunk = body[s:e]
        if ":" not in chunk.strip():
            continue
        prop, _, value = chunk.partition(":")
        if not prop.strip():
            continue
        lead = len(chunk) - len(chunk.lstrip())
        out.append(
            (prop.strip(), value.strip(), first_line + body[: s + lead].count("\n"))
        )
    return out


def parse(text):
    """Return a flat list of Rule objects. Nested @media/@supports/@container blocks are
    flattened
    with their preludes recorded in Rule.at; @keyframes bodies are skipped."""
    text = _blank_comments(text)
    rules = []

    def walk(pos, end, at):
        while pos < end:
            brace = text.find("{", pos, end)
            semi = text.find(";", pos, end)
            if brace == -1:
                return
            if semi != -1 and semi < brace and text[pos:semi].strip().startswith("@"):
                pos = semi + 1  # @import / @charset
                continue
            prelude = text[pos:brace].strip()
            depth, i = 1, brace + 1
            while i < end and depth:
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                i += 1
            body_start, body_end = brace + 1, i - 1
            line = text.count("\n", 0, brace) + 1
            if prelude.startswith("@"):
                name = prelude.split()[0] if prelude.split() else prelude
                if name in ("@media", "@supports", "@container", "@layer", "@scope"):
                    walk(body_start, body_end, at + [prelude])
                elif name.endswith("keyframes"):
                    pass
                else:  # e.g. @view-transition, @font-face
                    rules.append(
                        Rule(
                            prelude,
                            _split_decls(text[body_start:body_end], line),
                            at,
                            line,
                        )
                    )
            elif prelude:
                rules.append(
                    Rule(
                        prelude, _split_decls(text[body_start:body_end], line), at, line
                    )
                )
            pos = i

    walk(0, len(text), [])
    return rules


def colour_literals(value):
    """Colour literals in a declaration value: hex, colour functions and the named
    colours white/black.
    """
    found = [m.group(0) for m in HEX.finditer(value)]
    found += [m.group(0) for m in FUNC.finditer(value)]
    found += [m.group(0).lower() for m in NAMED.finditer(value)]
    return found


def custom_property_uses(value):
    return re.findall(r"var\(\s*(--[\w-]+)", value)
