"""Static guards for the Layout + chat-first Copilot UI (no browser needed).

Behavioural coverage of the Copilot page (async job cases A-D, composer keys,
Stop/Retry/Clear, mobile sheet, Live Context, a11y) lives in the Vitest suite
(`frontend/src/pages/Copilot.test.tsx`, run by `npm test` and CI). These checks
pin the structural contract so it cannot silently regress.

The sidebar must collapse below `lg` (1024px): a phone in "Desktop site" mode
reports a ~980px layout viewport, which passed the old `md` (768px) breakpoint.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FE = ROOT / "frontend"
LAYOUT = (FE / "src/components/Layout.tsx").read_text()
COPILOT = (FE / "src/pages/Copilot.tsx").read_text()
CTX = (FE / "src/components/copilot/LiveContext.tsx").read_text()
JOBS = (FE / "src/pages/copilotJobs.ts").read_text()
INDEX = (FE / "index.html").read_text()
APP = (FE / "src/App.tsx").read_text()


# ── Layout ──────────────────────────────────────────────────────────────────
def test_sidebar_collapses_below_lg_not_md():
    assert "hidden lg:flex flex-col w-56" in LAYOUT
    assert "hidden md:flex" not in LAYOUT and "md:hidden" not in LAYOUT
    assert "lg:hidden fixed bottom-0" in LAYOUT


def test_viewport_meta_is_responsive_and_zoomable():
    assert "width=device-width" in INDEX and "viewport-fit=cover" in INDEX
    assert "user-scalable=no" not in INDEX and "maximum-scale=1" not in INDEX


def test_layout_prevents_horizontal_overflow_and_uses_dynamic_vh():
    assert "h-[100dvh]" in LAYOUT and "overflow-x-hidden" in LAYOUT and "min-w-0" in LAYOUT


# ── Copilot page: exists, routed, chat-first ────────────────────────────────
def test_copilot_page_is_routed():
    assert "pages/Copilot" in APP and '"/copilot"' in APP


def test_chat_is_first_and_context_is_a_side_panel_on_desktop():
    assert COPILOT.index('data-testid="chat-panel"') < COPILOT.index('data-testid="context-aside"')
    assert "lg:basis-[70%]" in COPILOT and "lg:basis-[30%]" in COPILOT      # ~70/30 split
    assert "hidden lg:flex" in COPILOT                                        # aside is desktop-only
    # old dashboard-first components are gone from the page
    assert "BotContextCards" not in COPILOT and "AIDecisionPanel" not in COPILOT
    # context panel scrolls independently of the chat
    assert "overflow-y-auto" in COPILOT.split('data-testid="context-aside"')[1].split("</aside>")[0]


def test_composer_is_pinned_inside_the_chat_panel_and_log_scrolls():
    chat = COPILOT.split('data-testid="chat-panel"')[1].split("</section>")[0]
    assert 'role="log"' in chat and "overflow-y-auto" in chat
    assert chat.index('role="log"') < chat.index('data-testid="composer-input"')
    assert "shrink-0 border-t" in chat                                        # composer never scrolls away


def test_mobile_context_is_a_collapsible_dialog_not_ten_cards():
    assert 'role="dialog"' in COPILOT and 'aria-modal="true"' in COPILOT
    assert "lg:hidden" in COPILOT and "aria-expanded" in COPILOT
    assert "Escape" in COPILOT
    assert "max-h-[85dvh]" in COPILOT


def test_touch_targets_and_no_ios_focus_zoom():
    assert COPILOT.count("min-h-[44px]") >= 6
    assert "text-base sm:text-sm" in COPILOT
    for bad in ("w-screen", "min-w-[600px]", "min-w-[700px]", "w-[800px]"):
        assert bad not in COPILOT


def test_header_text_and_provider_vs_decision_engine_are_distinct():
    for text in ("Copilot AI", "Grounded in live bot state, trades and backtests",
                 "Observation &amp; explanation only", "No order placement"):
        assert text in COPILOT, text
    assert "backend offline" in COPILOT and "provider not configured" in COPILOT
    assert "Separate from the AI Trading Decision engine" in COPILOT
    assert "AI Trading Decision engine" in CTX and "separate from the Copilot provider" in CTX


def test_quick_prompts_are_the_required_six():
    for p in ("Why didn't the bot trade?", "Explain today's bot status", "Show today's trades",
              "Explain the latest rejection", "Explain the latest backtest", "What is my current risk?"):
        assert p in COPILOT, p


def test_send_stop_clear_retry_and_keyboard_contract():
    for label in ('aria-label="Send message"', 'aria-label="Stop generating"',
                  'aria-label="Clear conversation"', 'aria-label="Message Copilot"',
                  'aria-label="Retry this question"'):
        assert label in COPILOT, label
    assert "e.key === 'Enter' && !e.shiftKey" in COPILOT                      # Enter sends, Shift+Enter newline
    assert 'aria-live="polite"' in COPILOT


def test_live_context_uses_the_authoritative_endpoint_and_sections():
    assert "/api/copilot/context" in COPILOT
    for sec in ("runtime", "market", "scanner", "why", "risk", "positions", "trades",
                "backtest", "config", "ai"):
        assert f'id="{sec}"' in CTX, sec
    assert "ctx-problems" in CTX                                              # errors: prominent only when present
    assert "problems.length > 0" in CTX


# ── async contract (frontend side) ──────────────────────────────────────────
def test_async_endpoints_and_statuses_are_handled():
    assert "/api/copilot/chat/submit" in COPILOT
    assert "/api/copilot/chat/status/" in COPILOT and "/cancel" in COPILOT
    for st in ("queued", "thinking", "cancelling", "completed", "failed", "cancelled"):
        assert f"'{st}'" in JOBS, st
    assert "TERMINAL_STATUSES" in JOBS and "IN_FLIGHT_STATUSES" in JOBS
    # completed-in-POST must NOT produce a fake Thinking state
    assert "return null" in JOBS.split("export function stateFromSubmit")[1].split("export function fieldsFromStatus")[0]


def test_clear_starts_a_new_server_session():
    assert "setSessionId(fresh)" in COPILOT


# ── safety: Copilot UI can never place orders / change bot state ────────────
def test_copilot_ui_has_no_order_or_bot_control_api():
    import re
    urls = re.findall(r"['\"`](/api/[^'\"`$]*)", COPILOT + CTX + JOBS)
    assert urls, "expected /api/copilot/* calls"
    for u in urls:
        assert u.startswith("/api/copilot/"), u
    for bad in ("/api/orders", "/api/order", "/api/bot/", "/api/broker", "/api/execute", "place_order"):
        assert bad not in COPILOT + CTX + JOBS, bad


def test_dark_theme_preserved():
    assert "bg-[#141b2d]" in COPILOT and "bg-[#0a0e1a]" in LAYOUT
