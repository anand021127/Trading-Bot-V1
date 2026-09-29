"""Static guards for the Copilot/Layout responsive fix (no browser needed).

The sidebar must collapse below `lg` (1024px): a phone in "Desktop site" mode
reports a ~980px layout viewport, which passed the old `md` (768px) breakpoint
and left the 224px sidebar permanently visible."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAYOUT = (ROOT / "frontend/src/components/Layout.tsx").read_text()
COPILOT = (ROOT / "frontend/src/pages/Copilot.tsx").read_text()
INDEX = (ROOT / "frontend/index.html").read_text()


def test_sidebar_collapses_below_lg_not_md():
    assert "hidden lg:flex flex-col w-56" in LAYOUT
    assert "hidden md:flex" not in LAYOUT and "md:hidden" not in LAYOUT
    assert "lg:hidden fixed bottom-0" in LAYOUT          # bottom nav for phones/tablets


def test_viewport_meta_is_responsive_and_zoomable():
    assert "width=device-width" in INDEX and "viewport-fit=cover" in INDEX
    assert "user-scalable=no" not in INDEX and "maximum-scale=1" not in INDEX


def test_layout_prevents_horizontal_overflow_and_uses_dynamic_vh():
    assert "h-[100dvh]" in LAYOUT and "overflow-x-hidden" in LAYOUT and "min-w-0" in LAYOUT


def test_copilot_reflows_and_has_touch_sized_controls():
    assert "flex-wrap" in COPILOT and "wrap-anywhere" in COPILOT
    assert "min-h-[44px]" in COPILOT
    assert "text-base sm:text-sm" in COPILOT              # no iOS focus-zoom on the input
    assert "max-h-[60dvh] lg:max-h-none" in COPILOT       # no giant empty chat on phones
    for bad in ("w-screen", "min-w-[600px]", "min-w-[700px]", "w-[800px]"):
        assert bad not in COPILOT


def test_dark_theme_preserved():
    assert "bg-[#141b2d]" in COPILOT and "bg-[#0a0e1a]" in LAYOUT
