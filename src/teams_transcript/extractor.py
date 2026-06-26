from __future__ import annotations

import asyncio
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from teams_transcript import selectors as sel

DEFAULT_PROFILE_DIR = Path.home() / ".teams-transcript" / "playwright-profile"
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


def _extract_fileurl(url: str, *, debug: bool = False) -> str | None:
    """
    Return the decoded SharePoint URL embedded in a Teams meeting link, or None.

    Teams recap URLs embed a fileURL in various ways:
      - Top-level query param: &fileURL=https%3A%2F%2F...
      - Inside a JSON-encoded 'context' param: context={"fileURL":"https://..."}
      - Inside the URL fragment (Teams v2 hash-router): #/...?fileURL=...
      - With multiple layers of encoding

    We try progressively decoded versions of the URL and use both parse_qs and
    a regex fallback so we find the SharePoint URL regardless of encoding depth.
    """
    candidate = url
    seen: set[str] = set()

    for depth in range(4):
        if candidate in seen:
            break
        seen.add(candidate)

        parsed = urlparse(candidate)

        # Collect all query-string candidates: normal qs + anything after '?' in fragment
        query_parts: list[str] = [parsed.query]
        if '?' in parsed.fragment:
            query_parts.append(parsed.fragment.split('?', 1)[1])

        for qs in query_parts:
            if not qs:
                continue
            params = parse_qs(qs)

            # Case-insensitive key match (Teams uses 'fileURL' but be safe)
            for key, values in params.items():
                if key.lower() == 'fileurl':
                    val = unquote(values[0])
                    if val.startswith('http'):
                        if debug:
                            print(f"[fileURL] found via parse_qs at decode depth {depth}: {val[:80]}", flush=True)
                        return val

            # Also search all param values for a JSON object containing fileURL
            # (Teams sometimes puts {"fileURL":"..."} inside a 'context' param)
            for values in params.values():
                for val in values:
                    m = re.search(r'"fileURL"\s*:\s*"(https?://[^"]+)"', val, re.IGNORECASE)
                    if m:
                        result = unquote(m.group(1))
                        if debug:
                            print(f"[fileURL] found in JSON param at decode depth {depth}: {result[:80]}", flush=True)
                        return result

        # Regex fallback: find any SharePoint URL in the current decode level
        m = re.search(
            r'https?://[^\s\'"<>&]+\.sharepoint\.com[^\s\'"<>&]*',
            candidate,
            re.IGNORECASE,
        )
        if m:
            result = m.group(0)
            if debug:
                print(f"[fileURL] found via SharePoint regex at decode depth {depth}: {result[:80]}", flush=True)
            return result

        next_candidate = unquote(candidate)
        if next_candidate == candidate:
            break
        if debug:
            print(f"[fileURL] decode depth {depth}: no fileURL found, trying one more unquote pass", flush=True)
        candidate = next_candidate

    if debug:
        print("[fileURL] no SharePoint URL found in Teams link", flush=True)
    return None


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
    """Pause any playing HTML5 video elements as a best-effort measure."""
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


async def _lock_scroll_forward(page: Page, container, *, debug: bool = False) -> None:
    """
    Override scrollTop (and scrollTo) on the container element so it can only
    move forward — never backwards.  Teams keeps the transcript panel synced
    with video playback by repeatedly setting scrollTop to the position that
    matches the current timestamp.  This silently ignores any attempt to set
    scrollTop to a value lower than the highest value we've reached so far,
    meaning Teams' auto-sync calls are no-ops while our forward scrolls still
    work normally.
    """
    installed: bool = await page.evaluate("""
        (el) => {
            if (el.__scrollLocked) return false;
            el.__scrollLocked = true;

            // Find the descriptor on Element.prototype or HTMLElement.prototype
            const desc = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollTop')
                      || Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollTop');
            if (!desc || !desc.set) return false;

            let _floor = desc.get.call(el);

            Object.defineProperty(el, 'scrollTop', {
                configurable: true,
                enumerable: true,
                get() { return desc.get.call(this); },
                set(val) {
                    if (val >= _floor) {
                        _floor = val;
                        desc.set.call(this, val);
                    }
                    // val < _floor means Teams is trying to scroll us back — ignore.
                },
            });

            // scrollTo({top, behavior}) path
            const _origScrollTo = el.scrollTo.bind(el);
            el.scrollTo = function(optionsOrX, y) {
                const top = (optionsOrX !== null && typeof optionsOrX === 'object')
                    ? (optionsOrX.top ?? 0)
                    : (y ?? 0);
                if (top >= _floor) {
                    _floor = top;
                    _origScrollTo.apply(this, arguments);
                }
            };

            return true;
        }
    """, container)

    if debug:
        status = "installed" if installed else "skipped (already locked or unsupported)"
        print(f"[Scroll lock] {status}", flush=True)


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


async def _try_watch_in_browser(page: Page, *, debug: bool = False):
    """
    Click 'Watch in browser' if present and return the new tab it opens.

    Teams' v2 recap page sometimes shows a recording panel instead of an
    embedded transcript.  The 'Watch in browser' button opens the same content
    as a SharePoint recording page in a new tab — which exposes the transcript
    panel our extraction logic already handles.  Returns None if the button
    isn't found within 5 seconds.
    """
    combined = ", ".join(sel.WATCH_IN_BROWSER_SELECTORS)
    try:
        btn = await page.wait_for_selector(combined, timeout=5000)
        print("[Watch in browser] found — switching to SharePoint view...", flush=True)
        async with page.context.expect_page() as page_info:
            await btn.click()
        new_page = await page_info.value
        try:
            await new_page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            pass
        await asyncio.sleep(3)
        return new_page
    except Exception:
        if debug:
            print("[Watch in browser] button not found", flush=True)
        return None


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
    # If the Teams URL embeds a SharePoint fileURL, jump straight there — it
    # exposes the same transcript panel without any app-launch redirect dance.
    sp_url = _extract_fileurl(url, debug=debug)
    if sp_url:
        print(f"SharePoint URL found in Teams link — navigating directly...", flush=True)
        url = sp_url

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
        # Last resort: click "Watch in browser" which opens the SharePoint
        # recording view in a new tab — switch to that page and try again.
        new_page = await _try_watch_in_browser(page, debug=debug)
        if new_page is not None:
            page = new_page
            container = await _find_container(page, debug=debug, retries=3)

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

    # Pause the video as a best-effort measure.
    await _pause_video(page, debug=debug)

    # Scroll to the top, then lock the container so Teams cannot scroll it
    # backwards.  Teams continuously resets scrollTop to match the video's
    # current playback position; the lock silently ignores any set() call
    # that would move the scroll position backwards.
    await page.evaluate("(el) => { el.scrollTop = 0; }", container)
    await asyncio.sleep(0.5)
    await _lock_scroll_forward(page, container, debug=debug)
    print("Munching...", flush=True)

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


async def run_login(
    profile_dir: Path,
    channel: str = "msedge",
    *,
    await_browser_close: bool = False,
) -> None:
    """
    Open a browser window so the user can sign in to Teams and save the session.

    await_browser_close=True  — used by the GUI: returns automatically when the
                                user closes the browser window (no terminal needed).
    await_browser_close=False — used by the CLI: waits for Enter in the terminal.
    """
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            channel=channel,
            args=["--start-maximized"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://teams.microsoft.com")

        if await_browser_close:
            closed = asyncio.Event()
            ctx.on("close", lambda: closed.set())
            # Also fire if the user closes the last page manually
            for pg in ctx.pages:
                pg.on("close", lambda _: closed.set() if not ctx.pages else None)
            try:
                await asyncio.wait_for(closed.wait(), timeout=600)
            except asyncio.TimeoutError:
                pass
            # ctx may already be closed at this point; ignore the error
            try:
                await ctx.close()
            except Exception:
                pass
        else:
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
