"""
Teams Transcript Muncher — minimal GUI wrapper around the extraction engine.
"""
from __future__ import annotations

import asyncio
import io
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog

from teams_transcript.extractor import DEFAULT_PROFILE_DIR, run_extract, run_login
from teams_transcript.formatter import format_as_text

# ── Palette ─────────────────────────────────────────────────────────────────

_BG     = "#1a1a2e"   # dark navy background
_PANEL  = "#16213e"   # slightly lighter navy (status bar, secondary buttons)
_ENTRY  = "#0f3460"   # deep blue for input fields
_YELLOW = "#FFD700"   # Pac-Man gold — primary accent
_FG     = "#e0e0e0"   # light grey text

# ── Pac-Man drawing ──────────────────────────────────────────────────────────

def _draw_pacman(canvas: tk.Canvas) -> None:
    # Body with open mouth (mouth opens ~40° at top-right)
    canvas.create_arc(4, 4, 64, 64, start=40, extent=280,
                      fill=_YELLOW, outline=_YELLOW)
    # Eye
    canvas.create_oval(26, 13, 37, 24, fill=_BG, outline=_BG)
    # Dots to the right, shrinking to suggest they're being eaten
    for i, x in enumerate([84, 104, 120, 132]):
        r = max(7 - i * 1.5, 4)
        canvas.create_oval(x - r, 34 - r, x + r, 34 + r,
                           fill=_YELLOW, outline=_YELLOW)

# ── App ──────────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Teams Transcript Muncher")
        self.configure(bg=_BG)
        self.resizable(False, False)
        self._q: queue.Queue = queue.Queue()
        self._build_ui()
        self._poll()

    # ── UI construction ──────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # Header: Pac-Man + title
        hdr = tk.Frame(self, bg=_BG, padx=20, pady=18)
        hdr.pack(fill=tk.X)

        cvs = tk.Canvas(hdr, width=148, height=68, bg=_BG, highlightthickness=0)
        cvs.pack(side=tk.LEFT)
        _draw_pacman(cvs)

        title = tk.Frame(hdr, bg=_BG, padx=14)
        title.pack(side=tk.LEFT, fill=tk.Y, anchor=tk.CENTER)
        tk.Label(title, text="Teams Transcript",
                 font=("Segoe UI", 15, "bold"), bg=_BG, fg=_YELLOW,
                 anchor=tk.W).pack(fill=tk.X)
        tk.Label(title, text="Muncher",
                 font=("Segoe UI", 15, "bold"), bg=_BG, fg=_FG,
                 anchor=tk.W).pack(fill=tk.X)

        # Gold rule
        tk.Frame(self, bg=_YELLOW, height=2).pack(fill=tk.X)

        # Form
        frm = tk.Frame(self, bg=_BG, padx=20, pady=16)
        frm.pack(fill=tk.X)
        frm.columnconfigure(0, weight=1)

        self._url = self._field(frm, "Recap Link", row=0)
        self._out = self._field(frm, "Save to", row=2, pady_top=12, span=1)
        tk.Button(frm, text="Browse…", command=self._browse,
                  bg=_PANEL, fg=_FG, relief=tk.FLAT, padx=10,
                  font=("Segoe UI", 9), cursor="hand2") \
          .grid(row=3, column=1, padx=(8, 0), ipady=5, sticky=tk.EW)

        # Buttons
        btns = tk.Frame(self, bg=_BG, padx=20, pady=6)
        btns.pack(fill=tk.X)

        self._go = tk.Button(btns, text="Fetch Transcript",
                             command=self._fetch,
                             bg=_YELLOW, fg=_BG,
                             font=("Segoe UI", 10, "bold"),
                             relief=tk.FLAT, padx=18, pady=9,
                             cursor="hand2")
        self._go.pack(side=tk.LEFT)

        self._si = tk.Button(btns, text="Sign In",
                             command=self._signin,
                             bg=_PANEL, fg=_FG,
                             font=("Segoe UI", 9),
                             relief=tk.FLAT, padx=14, pady=9,
                             cursor="hand2")
        self._si.pack(side=tk.RIGHT)

        # Status bar
        sb = tk.Frame(self, bg=_PANEL, padx=20, pady=10)
        sb.pack(fill=tk.X, pady=(8, 0))
        self._sv = tk.StringVar(value="Ready.")
        tk.Label(sb, textvariable=self._sv,
                 bg=_PANEL, fg=_FG,
                 font=("Segoe UI", 9),
                 anchor=tk.W, justify=tk.LEFT,
                 wraplength=460).pack(fill=tk.X)

    def _field(self, parent: tk.Frame, label: str, *,
               row: int, pady_top: int = 0, span: int = 2) -> tk.StringVar:
        """Add a labelled Entry row; return its StringVar."""
        tk.Label(parent, text=label, bg=_BG, fg=_FG,
                 font=("Segoe UI", 9)) \
          .grid(row=row, column=0, columnspan=2,
                sticky=tk.W, pady=(pady_top, 3))
        var = tk.StringVar()
        tk.Entry(parent, textvariable=var,
                 bg=_ENTRY, fg=_FG, insertbackground=_FG,
                 relief=tk.FLAT, font=("Segoe UI", 9), width=54) \
          .grid(row=row + 1, column=0, columnspan=span,
                sticky=tk.EW, ipady=6)
        return var

    # ── Actions ──────────────────────────────────────────────────────────────

    def _browse(self) -> None:
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text file", "*.txt"), ("All files", "*.*")],
            title="Save transcript as…",
        )
        if path:
            self._out.set(path)

    def _signin(self) -> None:
        self._si.config(state=tk.DISABLED)
        self._status("Browser opened — sign in to Teams, then close the window.")
        DEFAULT_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

        def _run() -> None:
            try:
                asyncio.run(run_login(DEFAULT_PROFILE_DIR, await_browser_close=True))
                self._q.put(("ok", "Signed in. You can now fetch transcripts."))
            except Exception as exc:
                self._q.put(("err", f"Sign-in failed: {exc}"))
            finally:
                self._q.put(("enable", "_si"))

        threading.Thread(target=_run, daemon=True).start()

    def _fetch(self) -> None:
        url = self._url.get().strip()
        out = self._out.get().strip()

        if not url:
            self._status("Please enter a recap link.")
            return
        if not out:
            self._status("Please choose an output file location.")
            return
        if not DEFAULT_PROFILE_DIR.exists():
            self._status("Not signed in — click Sign In first.")
            return

        self._go.config(state=tk.DISABLED)
        self._status("Starting…")

        def _run() -> None:
            # Forward stdout into the status bar so the user gets live progress.
            class _Fwd(io.TextIOBase):
                def write(_, text: str) -> int:   # noqa: N805
                    if text.strip():
                        self._q.put(("status", text.strip()))
                    return len(text)
                def flush(_) -> None: pass         # noqa: N805

            real_out = sys.stdout
            sys.stdout = _Fwd()
            try:
                entries = asyncio.run(
                    run_extract(url, DEFAULT_PROFILE_DIR, headless=True)
                )
                if entries:
                    Path(out).write_text(format_as_text(entries), encoding="utf-8")
                    self._q.put(("ok", f"Done! {len(entries)} entries saved to {out}"))
                else:
                    self._q.put(("err", "No transcript found — check the link and try again."))
            except Exception as exc:
                self._q.put(("err", str(exc)))
            finally:
                sys.stdout = real_out
                self._q.put(("enable", "_go"))

        threading.Thread(target=_run, daemon=True).start()

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _status(self, msg: str) -> None:
        self._sv.set(msg)

    def _poll(self) -> None:
        try:
            while True:
                kind, val = self._q.get_nowait()
                if kind == "status":
                    self._status(val)
                elif kind == "ok":
                    self._status(val)
                elif kind == "err":
                    self._status(f"⚠  {val}")
                elif kind == "enable":
                    getattr(self, val).config(state=tk.NORMAL)
        except queue.Empty:
            pass
        self.after(100, self._poll)


# ── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    # Ensure stdout/stderr are never None in windowed PyInstaller builds.
    if sys.stdout is None:
        sys.stdout = io.StringIO()
    if sys.stderr is None:
        sys.stderr = io.StringIO()

    # Playwright requires ProactorEventLoop on Windows.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    App().mainloop()


if __name__ == "__main__":
    main()
