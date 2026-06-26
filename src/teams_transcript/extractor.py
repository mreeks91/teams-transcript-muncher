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
# How many consecutive steps where scrollTop + clientHeight >= scrollHeight
# must fire before we conclude we've reached the true end of the list.
# At 700 ms/step this is ~7 seconds — enough to ride out a slow batch load.
BOTTOM_STREAK_REQUIRED = 10
# Safety-net: stop if no new entries appear for this many consecutive steps
# even though scroll appears to still be moving (catches broken containers).
NO_NEW_SAFETY_LIMIT = 30

# Matches visible timestamps like "0:04", "1:23", "1:23:45"
_TIME_RE = re.compile(r'\b(\d{1,2}:\d{2}(?::\d{2})?)\b')
# Matches accessibility duration text injected by Teams for screen readers,
# e.g. "0 minutes 4 seconds", "1 hour 2 minutes 3 seconds"
_DURATION_RE = re.compile(
    r'\d+\s+(?:hours?|minutes?|seconds?)(?:\s+\d+\s+(?:hours?|minutes?|seconds?))*',
    re.IGNORECASE,
)
# System events that are not transcript speech
_SYSTEM_MSG_RE = re.compile(
    r'started transcription|stopped transcription|recording started|recording stopped',
    re.IGNORECASE,
)
# The final item Teams always appends — definitive proof we've reached the end.
_END_SENTINEL_RE = re.compile(r'stopped transcription', re.IGNORECASE)


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
    Find the transcript scroll container without relying on data-tid attributes.

    Strategy 1: Walk up the DOM from the first [role=listitem] looking for the
    nearest ancestor with overflow:scroll/auto and a scrollable height. This is
    the direct parent scroll box of the transcript list.

    Strategy 2: If no scrollable ancestor exists, use the direct parent of the
    first listitem (the list might fit on screen now but grow as content loads).

    Strategy 3: Scan all scrollable elements on the page for one whose inner
    text contains a timestamp pattern.
    """
    result = await page.evaluate("""
        () => {
            const MARKER = 'data-transcript-scroll';

            // Strategy 1 & 2: walk up from the first listitem
            const firstItem = document.querySelector('[role="listitem"], [role="article"]');
            if (firstItem) {
                let el = firstItem.parentElement;
                while (el && el !== document.documentElement) {
                    const ov = window.getComputedStyle(el).overflowY;
                    if ((ov === 'scroll' || ov === 'auto') && el.scrollHeight > el.clientHeight + 10) {
                        el.setAttribute(MARKER, '1');
                        return 'ancestor';
                    }
                    el = el.parentElement;
                }
                // No scrollable ancestor — use direct parent as the scroll target
                const parent = firstItem.parentElement;
                if (parent && parent !== document.body && parent !== document.documentElement) {
                    parent.setAttribute(MARKER, '2');
                    return 'parent';
                }
            }

            // Strategy 3: any scrollable element whose text contains a timestamp
            const TIME_RE = /\\b\\d{1,2}:\\d{2}\\b/;
            for (const el of document.querySelectorAll('*')) {
                const ov = window.getComputedStyle(el).overflowY;
                if ((ov === 'scroll' || ov === 'auto') && el.scrollHeight > el.clientHeight + 10) {
                    if (TIME_RE.test(el.innerText || '')) {
                        el.setAttribute(MARKER, '3');
                        return 'scrollable';
                    }
                }
            }
            return null;
        }
    """)
    if result:
        el = await page.query_selector('[data-transcript-scroll]')
        if debug:
            print(f"  MATCH  [heuristic container, strategy={result}]", flush=True)
        return el
    return None


def _parse_flat_list(raw_texts: list[str]) -> dict[str, TranscriptEntry]:
    """
    Parse Teams' flat [role=listitem] transcript structure.

    Teams alternates between two item types:
      Header item  — speaker name + accessibility duration text + visible timestamp
                     e.g. "Julia Meisel0 minutes 4 seconds0:04"
      Content item — plain transcript text, no timestamp
                     e.g. "But this is what I wanted to say."

    We detect header items by the presence of a timestamp pattern (digits:digits),
    strip the accessibility duration string to recover the speaker name, and
    pair each content item with the preceding header's speaker and timestamp.
    """
    entries: dict[str, TranscriptEntry] = {}
    current_speaker = "Unknown"
    current_timestamp = ""

    for raw in raw_texts:
        raw = raw.strip()
        if not raw or _SYSTEM_MSG_RE.search(raw):
            continue

        time_matches = _TIME_RE.findall(raw)
        if time_matches:
            # Header item: extract speaker name and timestamp
            current_timestamp = time_matches[-1]
            speaker = _DURATION_RE.sub('', raw)
            speaker = _TIME_RE.sub('', speaker).strip()
            if speaker:
                current_speaker = speaker
        else:
            # Content item: actual transcript speech
            entry = TranscriptEntry(
                timestamp=current_timestamp,
                speaker=current_speaker,
                text=raw,
            )
            key = _entry_key(entry)
            entries.setdefault(key, entry)  # keep first-seen rendering

    return entries


async def _collect_visible(
    page: Page, container, *, debug: bool = False
) -> tuple[dict[str, TranscriptEntry], bool]:
    """
    Grab the innerText of every [role=listitem] currently in the DOM
    (scoped to the scroll container if one was found), then parse them
    with _parse_flat_list.

    Returns (entries, found_end_sentinel) where found_end_sentinel is True
    when the "[name] stopped transcription" item is currently in the DOM —
    meaning we have scrolled all the way to the last item.
    """
    if container is not None:
        raw_texts: list[str] = await page.evaluate(
            """(el) => [...el.querySelectorAll('[role="listitem"], [role="article"]')]
                       .map(e => e.innerText)""",
            container,
        )
    else:
        raw_texts = await page.evaluate(
            """() => [...document.querySelectorAll('[role="listitem"], [role="article"]')]
                     .map(e => e.innerText)"""
        )

    if debug:
        print(f"  [collect] {len(raw_texts)} raw items in DOM", flush=True)

    found_sentinel = any(_END_SENTINEL_RE.search(t) for t in raw_texts)
    return _parse_flat_list(raw_texts), found_sentinel


async def _pause_video(page: Page, *, debug: bool = False) -> None:
    """
    Pause any playing HTML5 video so Teams stops auto-scrolling the transcript
    panel to follow the current playback position.
    """
    count: int = await page.evaluate("""
        () => {
            let n = 0;
            for (const v of document.querySelectorAll('video')) {
                if (!v.paused) { v.pause(); n++; }
            }
            return n;
        }
    """)
    if count:
        print(f"Paused {count} video element(s).", flush=True)
    elif debug:
        print("[Video] no playing video found", flush=True)


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
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass
        await asyncio.sleep(2)  # let React finish rendering the list
        return True
    return False


async def extract_transcript(
    page: Page,
    url: str,
    *,
    debug: bool = False,
    scroll_pause_ms: int = DEFAULT_SCROLL_PAUSE_MS,
    timeout_secs: int = DEFAULT_TIMEOUT_SECS,
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

    # Pause the video so Teams doesn't auto-scroll the transcript panel back
    # to the current playback position while we're trying to read ahead.
    await _pause_video(page, debug=debug)

    await page.evaluate("(el) => { el.scrollTop = 0; }", container)
    await asyncio.sleep(0.5)

    all_entries: dict[str, TranscriptEntry] = {}
    no_new_streak = 0   # consecutive steps with zero new entries
    bottom_streak = 0   # consecutive steps where scroll is truly at the end
    step = 0

    while True:
        step += 1
        visible, found_sentinel = await _collect_visible(page, container, debug=False)

        new_count = 0
        for key, entry in visible.items():
            if key not in all_entries:
                all_entries[key] = entry
                new_count += 1

        if new_count > 0:
            no_new_streak = 0
            bottom_streak = 0
        else:
            no_new_streak += 1

        at_bottom = await page.evaluate(
            "(el) => el.scrollTop + el.clientHeight >= el.scrollHeight - 10",
            container,
        )
        if at_bottom:
            bottom_streak += 1
        else:
            bottom_streak = 0

        if debug:
            print(
                f"  Step {step:3d}: total={len(all_entries):4d}  new={new_count:3d}"
                f"  no_new={no_new_streak}  bottom_streak={bottom_streak}"
                f"  sentinel={'YES' if found_sentinel else 'no'}",
                flush=True,
            )

        if screenshot_dir is not None:
            await page.screenshot(path=str(screenshot_dir / f"debug_scroll_{step:04d}.png"))

        # Primary exit: the "[name] stopped transcription" sentinel is in the
        # DOM — we've scrolled to the very last item.
        if found_sentinel:
            break

        # Fallback exit: stuck at the bottom for many steps with no sentinel
        # (e.g. a meeting where transcription was never formally stopped).
        if bottom_streak >= BOTTOM_STREAK_REQUIRED:
            break

        # Safety exit: something is wrong with the container.
        if no_new_streak >= NO_NEW_SAFETY_LIMIT:
            print(
                f"  Warning: no new entries after {NO_NEW_SAFETY_LIMIT} steps; stopping early.",
                file=sys.stderr,
                flush=True,
            )
            break

        # Scroll down
        await page.evaluate(
            "(el) => { el.scrollTop += el.clientHeight * 0.8; }",
            container,
        )
        await asyncio.sleep(scroll_pause_ms / 1000)

    total = len(all_entries)
    print(f"Done. Collected {total} transcript entr{'y' if total == 1 else 'ies'}.", flush=True)

    return sorted(all_entries.values(), key=lambda e: _parse_seconds(e.timestamp))


async def diagnose_page(
    page: Page,
    url: str,
    *,
    timeout_secs: int = DEFAULT_TIMEOUT_SECS,
) -> None:
    """
    Navigate to the URL and dump diagnostic info to stdout + a screenshot.
    Use this to find the correct data-tid values after a Teams DOM update.
    """
    print(f"Navigating to {url}", flush=True)
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_secs * 1000)
    await _bypass_app_launch_page(page, debug=True)
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_secs * 1000)
    except Exception:
        pass
    await _try_navigate_to_transcript_tab(page, debug=True)
    # Extra settle time for React to finish rendering
    await asyncio.sleep(3)

    print(f"\nPage title : {await page.title()}", flush=True)
    print(f"Page URL   : {page.url}", flush=True)

    # All data-tid values on the page, sorted by frequency
    tids: list[tuple[str, int]] = await page.evaluate("""
        () => {
            const counts = {};
            for (const el of document.querySelectorAll('[data-tid]')) {
                const t = el.getAttribute('data-tid');
                counts[t] = (counts[t] || 0) + 1;
            }
            return Object.entries(counts).sort((a, b) => b[1] - a[1]);
        }
    """)
    print(f"\n=== data-tid values on page ({len(tids)} unique) ===", flush=True)
    for tid, count in tids:
        print(f"  {count:4d}x  {tid}", flush=True)

    # Iframes (transcript might be inside one)
    iframes = await page.query_selector_all("iframe")
    if iframes:
        print(f"\n=== iframes ({len(iframes)}) ===", flush=True)
        for i, frame_el in enumerate(iframes):
            src = await frame_el.get_attribute("src") or "(no src)"
            name = await frame_el.get_attribute("name") or ""
            print(f"  [{i}] name={name!r:20s}  src={src[:80]}", flush=True)

    # Container selector probe
    print("\n=== Container selectors ===", flush=True)
    container = None
    container_sel_used = None
    for s in sel.CONTAINER_SELECTORS:
        els = await page.query_selector_all(s)
        hit = bool(els)
        print(f"  {'MATCH' if hit else 'miss ':5s}  {s!r} ({len(els)})", flush=True)
        if hit and container is None:
            container = els[0]
            container_sel_used = s

    # Item selector probe, scoped to the first matching container (or full page)
    scope_label = f"container {container_sel_used!r}" if container else "full page (no container)"
    print(f"\n=== Item selectors (scope: {scope_label}) ===", flush=True)
    scope = container if container is not None else page
    for s in sel.ITEM_SELECTORS:
        els = await scope.query_selector_all(s)
        hit = bool(els)
        print(f"  {'MATCH' if hit else 'miss ':5s}  {s!r} ({len(els)})", flush=True)
        if hit:
            for el in els[:3]:
                txt = (await el.inner_text()).strip().replace("\n", " | ")
                print(f"           → {txt[:120]!r}", flush=True)

    # Screenshot
    shot = Path("diagnose_screenshot.png")
    await page.screenshot(path=str(shot), full_page=False)
    print(f"\nScreenshot: {shot.resolve()}", flush=True)


async def run_diagnose(
    url: str,
    profile_dir: Path,
    *,
    channel: str = "msedge",
    use_live_edge_profile: bool = False,
    headless: bool = False,
    timeout_secs: int = DEFAULT_TIMEOUT_SECS,
) -> None:
    edge_profile = Path.home() / "AppData" / "Local" / "Microsoft" / "Edge" / "User Data"
    async with async_playwright() as p:
        profile_to_use = str(edge_profile) if use_live_edge_profile else str(profile_dir)
        launch_args = ["--disable-features=ExternalProtocolDialog"]
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
            await diagnose_page(page, url, timeout_secs=timeout_secs)
        finally:
            await ctx.close()


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
