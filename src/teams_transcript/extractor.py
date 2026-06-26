from __future__ import annotations

import asyncio
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from teams_transcript import selectors as sel

DEFAULT_SCROLL_PAUSE_MS = 700
DEFAULT_TIMEOUT_SECS = 60
DEFAULT_STALL_THRESHOLD = 3


@dataclass
class TranscriptEntry:
    timestamp: str
    speaker: str
    text: str


def _parse_seconds(ts: str) -> int:
    parts = [int(p) for p in ts.strip().split(":") if p.strip().isdigit()]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return 0


def _entry_key(e: TranscriptEntry) -> str:
    return f"{e.timestamp}:{e.speaker}:{e.text[:40]}"


async def _try_selectors(
    page: Page, selector_list: list[str], *, scope=None, debug: bool = False
) -> tuple[str | None, list]:
    """Try each selector; return (winning_selector, elements)."""
    for s in selector_list:
        try:
            if scope is not None:
                elements = await scope.query_selector_all(s)
            else:
                elements = await page.query_selector_all(s)
            if elements:
                if debug:
                    print(f"  MATCH  {s!r:60s} ({len(elements)} element(s))", flush=True)
                return s, elements
            else:
                if debug:
                    print(f"  miss   {s!r}", flush=True)
        except Exception:
            if debug:
                print(f"  error  {s!r}", flush=True)
    return None, []


async def _find_container(page: Page, *, debug: bool = False, retries: int = 3):
    """Locate the transcript scroll container, retrying if Teams hasn't rendered it yet."""
    for attempt in range(retries):
        if debug:
            print(f"\n[Container selectors] attempt {attempt + 1}/{retries}:", flush=True)
        winning_sel, elements = await _try_selectors(page, sel.CONTAINER_SELECTORS, debug=debug)
        if elements:
            return elements[0]

        if debug:
            print("[Heuristic] trying scrollable containers...", flush=True)
        container = await _heuristic_container(page, debug=debug)
        if container:
            return container

        if attempt < retries - 1:
            print(f"  Transcript container not found yet, retrying in 2s...", flush=True)
            await asyncio.sleep(2)

    return None


async def _heuristic_container(page: Page, *, debug: bool = False):
    """
    Last-resort: find any scrollable element whose children contain timestamps
    matching HH:MM or HH:MM:SS. Returns the first match or None.
    """
    result = await page.evaluate("""
        () => {
            const TIME_RE = /\\b\\d{1,2}:\\d{2}(:\\d{2})?\\b/;
            const candidates = document.querySelectorAll('[role="list"], [role="feed"], [overflow-y]');
            for (const el of candidates) {
                const kids = el.querySelectorAll('[role="listitem"], [role="article"]');
                if (kids.length >= 2) {
                    const sample = [...kids].slice(0, 5).map(k => k.innerText).join(' ');
                    if (TIME_RE.test(sample)) {
                        el.setAttribute('data-transcript-heuristic', 'true');
                        return true;
                    }
                }
            }
            return false;
        }
    """)
    if result:
        el = await page.query_selector('[data-transcript-heuristic="true"]')
        if debug:
            print("  MATCH  [heuristic scrollable container]", flush=True)
        return el
    return None


async def _collect_visible(
    page: Page, container, *, debug: bool = False
) -> dict[str, TranscriptEntry]:
    """Extract all currently-rendered transcript entries from the container."""
    if debug:
        print("\n[Item selectors]:", flush=True)
    _item_sel, items = await _try_selectors(page, sel.ITEM_SELECTORS, scope=container, debug=debug)

    entries: dict[str, TranscriptEntry] = {}
    for item in items:
        timestamp = await _first_text(item, sel.TIMESTAMP_SELECTORS)
        speaker = await _first_text(item, sel.SPEAKER_SELECTORS)
        text = await _first_text(item, sel.TEXT_SELECTORS)

        if not text:
            continue

        if not re.search(r"\d{1,2}:\d{2}", timestamp or ""):
            timestamp = ""

        entry = TranscriptEntry(
            timestamp=timestamp or "",
            speaker=speaker or "Unknown",
            text=text.strip(),
        )
        key = _entry_key(entry)
        entries[key] = entry

    return entries


async def _first_text(element, selector_list: list[str]) -> str:
    """Return innerText of the first sub-element that matches and has content."""
    for s in selector_list:
        try:
            sub = await element.query_selector(s)
            if sub:
                text = (await sub.inner_text()).strip()
                if text:
                    return text
        except Exception:
            continue
    return ""


async def _bypass_app_launch_page(page: Page, *, debug: bool = False) -> None:
    """
    Auto-click the 'Use the web app instead' button if Teams redirects to its
    app-launch landing page instead of opening the web UI directly.
    All button selectors are tried simultaneously via a combined CSS selector,
    with a 5-second total timeout. If none are found the page is already on
    the web app and we proceed normally.
    """
    combined = ", ".join(sel.WEB_APP_BUTTON_SELECTORS)
    try:
        btn = await page.wait_for_selector(combined, timeout=5000)
        if debug:
            print("[App launch] redirect page detected — clicking through", flush=True)
        await btn.click()
        try:
            await page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            pass
    except Exception:
        if debug:
            print("[App launch] no redirect page detected", flush=True)


async def _try_navigate_to_transcript_tab(page: Page, *, debug: bool = False) -> bool:
    """If we land on a recap page without the transcript visible, click into the tab."""
    if debug:
        print("\n[Transcript tab selectors]:", flush=True)
    _winning, tabs = await _try_selectors(page, sel.TRANSCRIPT_TAB_SELECTORS, debug=debug)
    if tabs:
        await tabs[0].click()
        await page.wait_for_load_state("networkidle", timeout=10_000)
        return True
    return False


async def extract_transcript(
    page: Page,
    url: str,
    *,
    debug: bool = False,
    scroll_pause_ms: int = DEFAULT_SCROLL_PAUSE_MS,
    timeout_secs: int = DEFAULT_TIMEOUT_SECS,
    stall_threshold: int = DEFAULT_STALL_THRESHOLD,
    screenshot_dir: Path | None = None,
) -> list[TranscriptEntry]:
    print(f"Navigating to {url}", flush=True)
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_secs * 1000)

    # Handle the "Open in Teams app?" redirect page before waiting for full load.
    await _bypass_app_launch_page(page, debug=debug)

    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_secs * 1000)
    except Exception:
        pass

    container = await _find_container(page, debug=debug)

    if container is None:
        await _try_navigate_to_transcript_tab(page, debug=debug)
        container = await _find_container(page, debug=debug, retries=2)

    if container is None:
        print(
            "\nERROR: Could not find the transcript panel.\n"
            "Tips:\n"
            "  1. Make sure the URL opens a meeting recap/transcript view.\n"
            "  2. Run with --debug to see which selectors were tried.\n"
            "  3. The Teams DOM may have changed — update selectors.py with\n"
            "     the new data-tid values found via browser DevTools.",
            file=sys.stderr,
        )
        return []

    print("Transcript container found. Scrolling to collect entries...", flush=True)
    await page.evaluate("(el) => { el.scrollTop = 0; }", container)
    await asyncio.sleep(0.5)

    all_entries: dict[str, TranscriptEntry] = {}
    stalls = 0
    step = 0

    while stalls < stall_threshold:
        step += 1
        visible = await _collect_visible(page, container, debug=False)

        new_count = 0
        for key, entry in visible.items():
            if key not in all_entries:
                all_entries[key] = entry
                new_count += 1

        if debug:
            print(
                f"  Step {step:3d}: total={len(all_entries):4d}  new={new_count:3d}  stalls={stalls}",
                flush=True,
            )

        if screenshot_dir is not None:
            await page.screenshot(path=str(screenshot_dir / f"debug_scroll_{step:04d}.png"))

        if new_count == 0:
            stalls += 1
        else:
            stalls = 0

        at_bottom = await page.evaluate(
            """
            (el) => {
                const before = el.scrollTop;
                el.scrollTop += el.clientHeight * 0.8;
                return Math.abs(el.scrollTop - before) < 2;
            }
            """,
            container,
        )
        await asyncio.sleep(scroll_pause_ms / 1000)

        if at_bottom:
            stalls += 1

    total = len(all_entries)
    print(f"Done. Collected {total} transcript entr{'y' if total == 1 else 'ies'}.", flush=True)

    return sorted(all_entries.values(), key=lambda e: _parse_seconds(e.timestamp))


async def run_login(profile_dir: Path, channel: str = "msedge") -> None:
    """Open a browser window so the user can sign in to Teams; saves the session."""
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            channel=channel,
            args=["--start-maximized"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://teams.microsoft.com")
        print(
            "\nBrowser opened. Sign in to Microsoft Teams (complete MFA if prompted).\n"
            "When the Teams home page is fully loaded, press Enter here to save the session.",
            flush=True,
        )
        await asyncio.to_thread(input, "")
        await ctx.close()
    print(f"Session saved to: {profile_dir}", flush=True)


async def run_extract(
    url: str,
    profile_dir: Path,
    *,
    channel: str = "msedge",
    use_live_edge_profile: bool = False,
    headless: bool = False,
    debug: bool = False,
    scroll_pause_ms: int = DEFAULT_SCROLL_PAUSE_MS,
    timeout_secs: int = DEFAULT_TIMEOUT_SECS,
    screenshot_dir: Path | None = None,
) -> list[TranscriptEntry]:
    edge_profile = Path.home() / "AppData" / "Local" / "Microsoft" / "Edge" / "User Data"

    async with async_playwright() as p:
        profile_to_use = str(edge_profile) if use_live_edge_profile else str(profile_dir)

        if use_live_edge_profile:
            print(
                "Using live Edge profile. Make sure Edge is completely closed before running.",
                flush=True,
            )

        launch_args = [
            # Suppress the OS-level "Open Microsoft Teams?" protocol-handler dialog
            # when navigating to teams.microsoft.com links in non-headless mode.
            "--disable-features=ExternalProtocolDialog",
        ]
        if not headless:
            launch_args.append("--start-maximized")

        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=profile_to_use,
            headless=headless,
            channel=channel,
            args=launch_args,
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        try:
            entries = await extract_transcript(
                page,
                url,
                debug=debug,
                scroll_pause_ms=scroll_pause_ms,
                timeout_secs=timeout_secs,
                screenshot_dir=screenshot_dir,
            )
        finally:
            await ctx.close()

    return entries
