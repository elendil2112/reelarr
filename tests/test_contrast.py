"""Every text colour stays readable (WCAG AA) in both themes."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import contrast


def test_palette_meets_aa():
    assert contrast.failures(contrast.CSS.read_text()) == []


def test_old_palette_would_fail():
    css = contrast.CSS.read_text().replace("#89621E", "#B97F1E")
    assert any(f[1] == "amber" for f in contrast.failures(css))
