"""Simple colorized logging for console output."""

from __future__ import annotations

import sys
from datetime import datetime
from typing import Final

from colorama import Fore, Style, init as colorama_init


colorama_init()


def _timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def info(message: str) -> None:
    sys.stdout.write(f"{Fore.CYAN}[{_timestamp()}] {message}{Style.RESET_ALL}\n")
    sys.stdout.flush()


def step(step_name: str, status: str, detail: str | None = None) -> None:
    accent = Fore.MAGENTA if status == "start" else Fore.GREEN if status == "success" else Fore.YELLOW
    base = f"STEP[{step_name}] {status.upper()}"
    if detail:
        base = f"{base}: {detail}"
    sys.stdout.write(f"{accent}[{_timestamp()}] {base}{Style.RESET_ALL}\n")
    sys.stdout.flush()


def success(message: str) -> None:
    sys.stdout.write(f"{Fore.GREEN}[{_timestamp()}] {message}{Style.RESET_ALL}\n")
    sys.stdout.flush()


def warning(message: str) -> None:
    sys.stdout.write(f"{Fore.YELLOW}[{_timestamp()}] {message}{Style.RESET_ALL}\n")
    sys.stdout.flush()


def error(message: str) -> None:
    sys.stderr.write(f"{Fore.RED}[{_timestamp()}] {message}{Style.RESET_ALL}\n")
    sys.stderr.flush()


def debug(message: str) -> None:
    """Debug level logging - only for development."""
    # Uncomment to enable debug logging
    # sys.stdout.write(f"{Fore.BLUE}[{_timestamp()}] DEBUG: {message}{Style.RESET_ALL}\n")
    # sys.stdout.flush()
    pass


