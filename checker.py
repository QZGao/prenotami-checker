#!/usr/bin/env python3
"""
PrenotaMi Schengen Visa Slot Checker + Auto-Booker

Monitors the Italian consulate's PrenotaMi appointment system for available
Schengen visa slots. When a slot is found, it attempts to book the earliest
available appointment and alerts you on Telegram.

This refactor keeps a persistent browser profile alive so anti-bot challenges
can be solved manually in the same browser session over VNC/noVNC before
resuming the loop with a Telegram /resume command.
"""

from __future__ import annotations

import argparse

from prenotami_checker.config import build_config, configure_logging


def main() -> None:
    parser = argparse.ArgumentParser(description="PrenotaMi Schengen Visa Auto-Booker")
    parser.add_argument("--loop", action="store_true", help="Run in continuous loop mode")
    parser.add_argument("--once", action="store_true", help="Run a single check (default)")
    args = parser.parse_args()

    config = build_config()
    log = configure_logging(config.log_dir)
    config.validate()
    from prenotami_checker.runner import PrenotamiRunner

    runner = PrenotamiRunner(config)

    try:
        if args.loop:
            log.info(
                "Starting persistent auto-book loop (interval: %ss = %s min)...",
                config.check_interval,
                config.check_interval // 60,
            )
            runner.run_loop()
        else:
            runner.run_once()
    finally:
        runner.shutdown()


if __name__ == "__main__":
    main()
