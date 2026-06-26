"""
Teams Transcript Muncher -- minimal GUI wrapper around the extraction engine.
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

# ── Palette ──────────────────────────────────────────────────────────────────

_BG     = "#1a1a2e"
_PANEL  = "#16213e"
_ENTRY  = "#0f3460"
_YELLOW = "#FFD700"
_FG     = "#e0e0e0"

# ── Animation constants ───────────────────────────────────────────────────────

_FRAME_MS    = 55       # ~18 fps
_MOUTH_OPEN  = 280      # arc extent when fully open (80-deg gap)
_MOUTH_SHUT  = 359      # arc extent when fully closed (1-deg gap keeps eye logic simple)
_MOUTH_STEP  = 14       # degrees per frame
_DOT_SPEED   = 4        # pixels per frame
_DOT_R       = 5        # dot radius px
_DOT_GAP     = 22       # pixels between dot births
_DOT_EAT_X   = 68      # dots are consumed when centre crosses this x
_DOT_BIRTH_X = 152      # dots are born just off the right edge

# ── Canvas helpers ────────────────────────────────────────────────────────────

def _paint(canvas: tk.Canvas, extent: int, dots: list[float]) -> None:
    """Repaint the Pac-Man canvas with the given mouth extent and dot positions."""
    canvas.delete("all")
    # Body
    canvas.create_arc(4, 4, 64, 64, start=40, extent=extent,
                      fill=_YELLOW, outline=_YELLOW)
    # Eye (hide when mouth is almost shut so it looks like a circle)
    if extent < 350:
        canvas.create_oval(26, 13, 37, 24, fill=_BG, outline=_BG)
    # Dots
    for x in dots:
        canvas.create_oval(x - _DOT_R, 34 - _DOT_R, x + _DOT_R, 34 + _DOT_R,
                           fill=_YELLOW, outline=_YELLOW)


def _static_dots() -> list[float]:
    return [84.0, 104.0, 120.0, 132.0]


# ── App ───────────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Teams Transcript Muncher")
        self.configure(bg=_BG)
        self.resizable(False, False)
        self._q: queue.Queue = queue.Queue()

        # Animation state
        self._animating   = False
        self._mouth_ext   = float(_MOUTH_OPEN)
        self._mouth_dir   = float(_MOUTH_STEP)   # positive = closing
        self._dots: list[float] = []

        self._build_ui()
        self._poll()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        hdr = tk.Frame(self, bg=_BG, padx=20, pady=18)
        hdr.pack(fill=tk.X)

        self._cvs = tk.Canvas(hdr, width=_DOT_BIRTH_X + 4, height=68,
                              bg=_BG, highlightthickness=0)
        self._cvs.pack(side=tk.LEFT)
        _paint(self._cvs, _MOUTH_OPEN, _static_dots())

        title = tk.Frame(hdr, bg=_BG, padx=14)
        title.pack(side=tk.LEFT, fill=tk.Y, anchor=tk.CENTER)
        tk.Label(title, text="Teams Transcript",
                 font=("Segoe UI", 15, "bold"), bg=_BG, fg=_YELLOW,
                 anchor=tk.W).pack(fill=tk.X)
        tk.Label(title, text="Muncher",
                 font=("Segoe UI", 15, "bold"), bg=_BG, fg=_FG,
                 anchor=tk.W).pack(fill=tk.X)

        tk.Frame(self, bg=_YELLOW, height=2).pack(fill=tk.X)

        frm = tk.Frame(self, bg=_BG, padx=20, pady=16)
        frm.pack(fill=tk.X)
        frm.columnconfigure(0, weight=1)

        self._url = self._field(frm, "Recap Link", row=0)
        self._out = self._field(frm, "Save to", row=2, pady_top=12, span=1)
        tk.Button(frm, text="Browse...", command=self._browse,
                  bg=_PANEL, fg=_FG, relief=tk.FLAT, padx=10,
                  font=("Segoe UI", 9), cursor="hand2") \
          .grid(row=3, column=1, padx=(8, 0), ipady=5, sticky=tk.EW)

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

        sb = tk.Frame(self, bg=_PANEL, padx=20, pady=10)
        sb.pack(fill=tk.X, pady=(8, 0))
        self._sv = tk.StringVar(value="Ready.")
        tk.Label(sb, textvariable=self._sv,
                 bg=_PANEL, fg=_FG, font=("Segoe UI", 9),
                 anchor=tk.W, justify=tk.LEFT, wraplength=460).pack(fill=tk.X)

    def _field(self, parent: tk.Frame, label: str, *,
               row: int, pady_top: int = 0, span: int = 2) -> tk.StringVar:
        tk.Label(parent, text=label, bg=_BG, fg=_FG, font=("Segoe UI", 9)) \
          .grid(row=row, column=0, columnspan=2, sticky=tk.W, pady=(pady_top, 3))
        var = tk.StringVar()
        tk.Entry(parent, textvariable=var,
                 bg=_ENTRY, fg=_FG, insertbackground=_FG,
                 relief=tk.FLAT, font=("Segoe UI", 9), width=54) \
          .grid(row=row + 1, column=0, columnspan=span, sticky=tk.EW, ipady=6)
        return var

    # ── Animation ─────────────────────────────────────────────────────────────

    def _anim_start(self) -> None:
        if self._animating:
            return
        self._animating  = True
        self._mouth_ext  = float(_MOUTH_OPEN)
        self._mouth_dir  = float(_MOUTH_STEP)
        self._dots       = []
        self._anim_tick()

    def _anim_stop(self) -> None:
        self._animating = False
        # Restore static appearance on next tick (tick checks _animating first)

    def _anim_tick(self) -> None:
        if not self._animating:
            _paint(self._cvs, _MOUTH_OPEN, _static_dots())
            return

        # Advance mouth (open <-> shut cycle)
        self._mouth_ext += self._mouth_dir
        if self._mouth_ext >= _MOUTH_SHUT:
            self._mouth_ext = float(_MOUTH_SHUT)
            self._mouth_dir = -float(_MOUTH_STEP)
        elif self._mouth_ext <= _MOUTH_OPEN:
            self._mouth_ext = float(_MOUTH_OPEN)
            self._mouth_dir = float(_MOUTH_STEP)

        # Move dots left; drop any that Pac-Man ate
        self._dots = [x - _DOT_SPEED for x in self._dots if x - _DOT_SPEED > _DOT_EAT_X]

        # Spawn a new dot when the rightmost one has moved far enough inward
        if not self._dots or self._dots[-1] <= _DOT_BIRTH_X - _DOT_GAP:
            self._dots.append(float(_DOT_BIRTH_X))

        _paint(self._cvs, int(self._mouth_ext), self._dots)
        self.after(_FRAME_MS, self._anim_tick)

    # ── Actions ───────────────────────────────────────────────────────────────

    def _browse(self) -> None:
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text file", "*.txt"), ("All files", "*.*")],
            title="Save transcript as...",
        )
        if path:
            self._out.set(path)

    def _signin(self) -> None:
        self._si.config(state=tk.DISABLED)
        self._status("Browser opened -- sign in to Teams, then close the window.")
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
            self._status("Not signed in -- click Sign In first.")
            return

        self._go.config(state=tk.DISABLED)
        self._status("Starting...")
        self._anim_start()

        def _run() -> None:
            class _Fwd(io.TextIOBase):
                def write(_, text: str) -> int:  # noqa: N805
                    if text.strip():
                        self._q.put(("status", text.strip()))
                    return len(text)
                def flush(_) -> None: pass        # noqa: N805

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
                    self._q.put(("err", "No transcript found -- check the link and try again."))
            except Exception as exc:
                self._q.put(("err", str(exc)))
            finally:
                sys.stdout = real_out
                self._q.put(("enable", "_go"))

        threading.Thread(target=_run, daemon=True).start()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _status(self, msg: str) -> None:
        self._sv.set(msg)

    def _poll(self) -> None:
        try:
            while True:
                kind, val = self._q.get_nowait()
                if kind == "status":
                    self._status(val)
                elif kind == "ok":
                    self._anim_stop()
                    self._status(val)
                elif kind == "err":
                    self._anim_stop()
                    self._status(f"  {val}")
                elif kind == "enable":
                    getattr(self, val).config(state=tk.NORMAL)
        except queue.Empty:
            pass
        self.after(100, self._poll)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    if sys.stdout is None:
        sys.stdout = io.StringIO()
    if sys.stderr is None:
        sys.stderr = io.StringIO()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    App().mainloop()


if __name__ == "__main__":
    main()
