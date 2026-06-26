"""
Ordered selector chains for Teams transcript DOM elements.
Teams uses React with `data-tid` test-id attributes; these are more stable
than class names but can still change across app updates. Each list is tried
in order — first match wins. `--debug` prints which selector matched.

To update after a Teams DOM change: open DevTools on the recap page,
inspect the transcript panel, find the new data-tid values, and add them
to the front of the relevant list here.
"""

CONTAINER_SELECTORS: list[str] = [
    '[data-tid="virtualizedTranscriptList"]',
    '[data-tid="transcript-list"]',
    '[data-tid="transcript-container"]',
    '[data-tid="transcriptContainer"]',
    '[role="list"][aria-label*="transcript" i]',
    '[role="feed"]',
]

ITEM_SELECTORS: list[str] = [
    '[data-tid="transcript-item-wrapper"]',
    '[data-tid="transcriptItem"]',
    '[data-tid="transcript-item"]',
    '[role="listitem"]',
    '[role="article"]',
]

SPEAKER_SELECTORS: list[str] = [
    '[data-tid="transcript-item-speaker-name"]',
    '[data-tid="speakerName"]',
    '[data-tid="transcript-speaker"]',
    '[data-tid="displayName"]',
    'strong',
    '[class*="speaker" i]',
    '[class*="author" i]',
]

TIMESTAMP_SELECTORS: list[str] = [
    '[data-tid="transcript-item-timestamp"]',
    '[data-tid="timestamp"]',
    '[data-tid="startTime"]',
    'time',
    '[datetime]',
    '[class*="timestamp" i]',
    '[class*="time" i]',
]

TEXT_SELECTORS: list[str] = [
    '[data-tid="transcript-item-text"]',
    '[data-tid="transcriptText"]',
    '[data-tid="transcript-text"]',
    '[data-tid="messageContent"]',
    'p',
    '[class*="content" i]',
    '[class*="text" i]',
]

TRANSCRIPT_TAB_SELECTORS: list[str] = [
    '[data-tid="recap-tab-transcript"]',
    '[data-tid="transcript-tab"]',
    '[role="tab"][aria-label*="transcript" i]',
    'button[aria-label*="transcript" i]',
    'a[aria-label*="transcript" i]',
]

# "Watch in browser" button on the Teams v2 recap page.
# Clicking it opens the SharePoint recording view in a new tab, which exposes
# the same transcript panel our extraction logic already handles.
WATCH_IN_BROWSER_SELECTORS: list[str] = [
    'button:has-text("Watch in browser")',
    'a:has-text("Watch in browser")',
    '[aria-label*="Watch in browser" i]',
]

# Buttons/links on the Teams "Open in app?" landing page.
# Tried as a combined CSS selector so all are checked simultaneously.
WEB_APP_BUTTON_SELECTORS: list[str] = [
    '[data-tid="joinOnWeb"]',
    '[data-tid="use-web-app"]',
    '[data-tid="openInBrowser"]',
    'a:has-text("Use the web app instead")',
    'button:has-text("Use the web app instead")',
    'a:has-text("Continue on this browser")',
    'button:has-text("Continue on this browser")',
    'a:has-text("Open in browser")',
    'button:has-text("Open in browser")',
    'a:has-text("Use web app")',
    'button:has-text("Use web app")',
]
