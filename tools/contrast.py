"""Check every text colour in app/static/style.css against WCAG AA (4.5:1)
on the page background and both surface colours, in dark and light mode.

    python tools/contrast.py        # prints a table, exits 1 on a failure
"""
import re
import sys
from pathlib import Path

CSS = Path(__file__).resolve().parents[1] / "app" / "static" / "style.css"
TEXT = ("ink", "dim", "amber", "amber-hi", "teal", "rust", "violet")
BACKGROUNDS = ("bg", "surface", "surface-2")
AA = 4.5


def _lum(hexcol: str) -> float:
    h = hexcol.lstrip("#")
    c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def ratio(a: str, b: str) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def palettes(css: str) -> dict:
    """{'dark': {token: hex}, 'light': {...}} from the :root blocks."""
    def tokens(block):
        return dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})", block))
    dark = tokens(css[css.index(":root {"):css.index("}", css.index(":root {"))])
    i = css.index(':root[data-theme="light"]')
    light = {**dark, **tokens(css[i:css.index("}", i)])}
    return {"dark": dark, "light": light}


def failures(css: str) -> list:
    out = []
    for mode, p in palettes(css).items():
        for t in TEXT:
            for bg in BACKGROUNDS:
                r = ratio(p[t], p[bg])
                if r < AA:
                    out.append((mode, t, bg, round(r, 2)))
        if mode == "light":                        # white text on amber buttons
            r = ratio(p["on-accent"], p["amber"])
            if r < AA:
                out.append((mode, "on-accent", "amber", round(r, 2)))
    return out


if __name__ == "__main__":
    bad = failures(CSS.read_text())
    for mode, t, bg, r in bad:
        print(f"FAIL {mode:5} --{t} on --{bg}: {r}:1 (needs {AA})")
    print("all text colours pass AA" if not bad else f"{len(bad)} failure(s)")
    sys.exit(1 if bad else 0)
