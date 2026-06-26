from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from teams_transcript.extractor import run_extract, run_login
from teams_transcript.formatter import format_as_text

DEFAULT_PROFILE_DIR = Path.home() / ".teams-transcript" / "playwright-profile"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="teams-transcript",
        description=(
            "Extract a Microsoft Teams meeting transcript from the recap page.\n\n"
            "First-time setup:\n"
            "  teams-transcript --login\n\n"
            "Usage:\n"
            "  teams-transcript \"https://teams.microsoft.com/...\" -o transcript.txt"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("url", nargs="?", help="Teams recap page URL")
    p.add_argument("-o", "--output", metavar="PATH", help="Write output to file (default: stdout)")
    p.add_argument(
        "--login",
        action="store_true",
        help="Open browser for one-time Teams sign-in, then exit",
    )
    p.add_argument(
        "--profile-dir",
        metavar="PATH",
        default=str(DEFAULT_PROFILE_DIR),
        help=f"Playwright browser profile directory (default: {DEFAULT_PROFILE_DIR})",
    )
    p.add_argument(
        "--use-edge-profile",
        action="store_true",
        help=(
            "Use your existing Edge installation profile instead of the dedicated "
            "Playwright profile. Edge must be completely closed before running."
        ),
    )
    p.add_argument(
        "--browser",
        choices=["edge", "chrome", "chromium"],
        default="edge",
        help="Browser to use (default: edge)",
    )
    p.add_argument(
        "--headless",
        action="store_true",
        help=(
            "Run the browser without a visible window. "
            "Also eliminates the OS 'Open Teams app?' dialog. "
            "Requires a saved session (run --login first)."
        ),
    )
    p.add_argument(
        "--debug",
        action="store_true",
        help="Print selector probes, entry counts per step, and save scroll screenshots",
    )
    p.add_argument(
        "--scroll-pause",
        metavar="MS",
        type=int,
        default=700,
        help="Pause between scroll steps in milliseconds (default: 700)",
    )
    p.add_argument(
        "--timeout",
        metavar="SECS",
        type=int,
        default=60,
        help="Page load timeout in seconds (default: 60)",
    )
    p.add_argument(
        "--group-by-speaker",
        action="store_true",
        help="Merge consecutive lines from the same speaker into blocks",
    )
    return p


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    profile_dir = Path(args.profile_dir)
    channel_map = {"edge": "msedge", "chrome": "chrome", "chromium": "chromium"}
    channel = channel_map[args.browser]

    if args.login:
        profile_dir.mkdir(parents=True, exist_ok=True)
        asyncio.run(run_login(profile_dir, channel=channel))
        return

    if not args.url:
        parser.error("url is required unless --login is specified")

    if not args.use_edge_profile and not profile_dir.exists():
        print(
            "No saved session found. Run this first to sign in:\n\n"
            "  teams-transcript --login\n",
            file=sys.stderr,
        )
        sys.exit(1)

    screenshot_dir = Path(".") if args.debug else None

    entries = asyncio.run(
        run_extract(
            args.url,
            profile_dir,
            channel=channel,
            use_live_edge_profile=args.use_edge_profile,
            headless=args.headless,
            debug=args.debug,
            scroll_pause_ms=args.scroll_pause,
            timeout_secs=args.timeout,
            screenshot_dir=screenshot_dir,
        )
    )

    if not entries:
        sys.exit(1)

    output = format_as_text(entries, group_by_speaker=args.group_by_speaker)

    if args.output:
        out_path = Path(args.output)
        out_path.write_text(output, encoding="utf-8")
        print(f"Transcript written to: {out_path}", flush=True)
    else:
        print(output)
