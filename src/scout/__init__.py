"""Rental-investment scout: ingest property alert emails, analyse, deliver."""

import sys

__version__ = "0.1.0"


def configure_stdio() -> None:
    """Force UTF-8 on stdout/stderr, for every command-line entry point.

    French listings are full of €, m² and accents, and a Windows console
    defaults to cp1252: log lines come out as mojibake and argparse's --help
    dies outright on a UnicodeEncodeError. `errors="replace"` keeps a genuinely
    unencodable character from ever crashing a run.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            # Not a real terminal (pytest capture, a pipe on some platforms).
            pass
