/**
 * Teams Transcript Extractor — Browser DevTools Snippet
 *
 * Zero-install alternative to the Python CLI. Downloads the full transcript
 * as a text file using a browser-native blob URL, which is not subject to
 * the Teams org policy that blocks the native "Download transcript" button.
 *
 * Usage:
 *   1. Open the Teams meeting recap in your browser and navigate to the
 *      Transcript tab so the transcript list is visible.
 *   2. Press F12 to open DevTools.
 *   3. Click the "Console" tab.
 *   4. Paste this entire script and press Enter.
 *   5. A file named "teams-transcript.txt" will download automatically.
 *
 * If it prints "Could not find transcript container", run with DEBUG = true
 * (change the line below) to see which selectors were tried, then update
 * the selector lists to match what you see in the Elements panel.
 */

(async function extractTeamsTranscript() {
  const DEBUG = false;
  const SCROLL_PAUSE_MS = 700;
  const STALL_THRESHOLD = 3;

  const CONTAINER_SELECTORS = [
    '[data-tid="virtualizedTranscriptList"]',
    '[data-tid="transcript-list"]',
    '[data-tid="transcript-container"]',
    '[data-tid="transcriptContainer"]',
    '[role="list"][aria-label*="transcript" i]',
    '[role="feed"]',
  ];

  const ITEM_SELECTORS = [
    '[data-tid="transcript-item-wrapper"]',
    '[data-tid="transcriptItem"]',
    '[data-tid="transcript-item"]',
    '[role="listitem"]',
    '[role="article"]',
  ];

  const SPEAKER_SELECTORS = [
    '[data-tid="transcript-item-speaker-name"]',
    '[data-tid="speakerName"]',
    '[data-tid="transcript-speaker"]',
    '[data-tid="displayName"]',
    'strong',
    '[class*="speaker" i]',
    '[class*="author" i]',
  ];

  const TIMESTAMP_SELECTORS = [
    '[data-tid="transcript-item-timestamp"]',
    '[data-tid="timestamp"]',
    '[data-tid="startTime"]',
    'time',
    '[datetime]',
    '[class*="timestamp" i]',
    '[class*="time" i]',
  ];

  const TEXT_SELECTORS = [
    '[data-tid="transcript-item-text"]',
    '[data-tid="transcriptText"]',
    '[data-tid="transcript-text"]',
    '[data-tid="messageContent"]',
    'p',
    '[class*="content" i]',
    '[class*="text" i]',
  ];

  const delay = (ms) => new Promise((r) => setTimeout(r, ms));

  function firstText(element, selectorList) {
    for (const s of selectorList) {
      try {
        const sub = element.querySelector(s);
        if (sub) {
          const t = sub.innerText.trim();
          if (t) return t;
        }
      } catch (_) {}
    }
    return "";
  }

  function findContainer() {
    for (const s of CONTAINER_SELECTORS) {
      const el = document.querySelector(s);
      if (el) {
        if (DEBUG) console.log(`Container: ${s}`);
        return el;
      }
    }
    // Heuristic: find scrollable list with timestamp-like text in children
    const TIME_RE = /\b\d{1,2}:\d{2}(:\d{2})?\b/;
    const candidates = document.querySelectorAll('[role="list"], [role="feed"]');
    for (const el of candidates) {
      const kids = el.querySelectorAll('[role="listitem"], [role="article"]');
      if (kids.length >= 2) {
        const sample = [...kids].slice(0, 5).map((k) => k.innerText).join(" ");
        if (TIME_RE.test(sample)) {
          if (DEBUG) console.log("Container: [heuristic]");
          return el;
        }
      }
    }
    return null;
  }

  function collectVisible(container) {
    const entries = new Map();
    let itemSel = null;
    for (const s of ITEM_SELECTORS) {
      const els = container.querySelectorAll(s);
      if (els.length) { itemSel = s; break; }
    }
    if (!itemSel) return entries;

    for (const item of container.querySelectorAll(itemSel)) {
      const ts = firstText(item, TIMESTAMP_SELECTORS);
      const speaker = firstText(item, SPEAKER_SELECTORS) || "Unknown";
      const text = firstText(item, TEXT_SELECTORS);
      if (!text) continue;
      const key = `${ts}:${speaker}:${text.slice(0, 40)}`;
      if (!entries.has(key)) {
        entries.set(key, { ts, speaker, text });
      }
    }
    return entries;
  }

  function parseSeconds(ts) {
    const parts = ts.split(":").map(Number).filter((n) => !isNaN(n));
    if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
    if (parts.length === 2) return parts[0] * 60 + parts[1];
    return 0;
  }

  // ---- Main ----

  const container = findContainer();
  if (!container) {
    console.error(
      "Could not find the transcript container.\n" +
      "Make sure the Transcript tab is open and the list is visible.\n" +
      "Set DEBUG = true at the top of the script to see selector details."
    );
    return;
  }

  container.scrollTop = 0;
  await delay(500);

  const allEntries = new Map();
  let stalls = 0;
  let step = 0;

  while (stalls < STALL_THRESHOLD) {
    step++;
    const visible = collectVisible(container);
    let newCount = 0;
    for (const [key, entry] of visible) {
      if (!allEntries.has(key)) {
        allEntries.set(key, entry);
        newCount++;
      }
    }

    if (DEBUG) {
      console.log(`Step ${step}: total=${allEntries.size} new=${newCount} stalls=${stalls}`);
    }

    if (newCount === 0) {
      stalls++;
    } else {
      stalls = 0;
    }

    const prevTop = container.scrollTop;
    container.scrollTop += container.clientHeight * 0.8;
    await delay(SCROLL_PAUSE_MS);
    if (Math.abs(container.scrollTop - prevTop) < 2) {
      stalls++;
    }
  }

  const sorted = [...allEntries.values()].sort(
    (a, b) => parseSeconds(a.ts) - parseSeconds(b.ts)
  );

  const lines = sorted.map((e) =>
    e.ts ? `[${e.ts}] ${e.speaker}: ${e.text}` : `${e.speaker}: ${e.text}`
  );
  const content = lines.join("\n");

  const blob = new Blob([content], { type: "text/plain" });
  const url = URL.createObjectURL(blob);
  const a = Object.assign(document.createElement("a"), {
    href: url,
    download: "teams-transcript.txt",
  });
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);

  console.log(`Done. Downloaded ${sorted.length} transcript entries as teams-transcript.txt`);
})();
