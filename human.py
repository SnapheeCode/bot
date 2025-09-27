"""Utilities for generating human-like interactions in Playwright."""

from __future__ import annotations

import asyncio
import math
import random
from typing import Iterable, Optional, Tuple

from playwright.async_api import Locator, Page


def _rand(range_pair: Tuple[float, float]) -> float:
    """Return a float within the passed inclusive range."""

    low, high = range_pair
    return random.uniform(low, high)


async def pause(range_pair: Tuple[int, int]) -> None:
    """Sleep for a random amount of milliseconds within the range."""

    delay_ms = _rand(range_pair)
    await asyncio.sleep(delay_ms / 1000)


async def jitter(range_pair: Tuple[int, int]) -> float:
    """Return a random float in range while awaiting the duration."""

    delay_ms = _rand(range_pair)
    await asyncio.sleep(delay_ms / 1000)
    return delay_ms


async def human_mouse_move(
    page: Page,
    target_bbox: Tuple[float, float, float, float],
    steps_range: Tuple[int, int] = (12, 24),
    jitter: float = 4.0,
) -> None:
    """Move the mouse to a random spot inside the target bounding box."""

    x, y, width, height = target_bbox
    dest_x = random.uniform(x + 5, x + width - 5)
    dest_y = random.uniform(y + 5, y + height - 5)

    current = page.mouse
    steps = max(5, int(_rand((steps_range[0], steps_range[1]))))

    # Create a bezier-like path with jitter.
    points: list[tuple[float, float]] = []
    for i in range(steps):
        t = (i + 1) / steps
        cos_t = math.cos(t * math.pi / 2)
        sin_t = math.sin(t * math.pi / 2)
        intermediate_x = dest_x - cos_t * (dest_x - x) + random.uniform(-jitter, jitter)
        intermediate_y = dest_y - sin_t * (dest_y - y) + random.uniform(-jitter, jitter)
        points.append((intermediate_x, intermediate_y))

    for idx, (px, py) in enumerate(points):
        await current.move(px, py, steps=1)
        if idx % 3 == 0:
            await pause((8, 18))


async def human_click(locator: Locator) -> None:
    """Click locator at a random point using a human-like mouse movement."""

    page = locator.page
    bbox = await locator.bounding_box()
    if not bbox:
        raise RuntimeError("Element has no bounding box to click")

    await human_mouse_move(page, (bbox["x"], bbox["y"], bbox["width"], bbox["height"]))
    await pause((20, 90))
    offset_x = random.uniform(6, max(6, bbox["width"] - 6))
    offset_y = random.uniform(6, max(6, bbox["height"] - 6))
    await locator.click(position={"x": offset_x, "y": offset_y})


async def _apply_scroll(page: Page, container: Optional[Locator], delta: float) -> None:
    if container is not None:
        try:
            await container.hover()
        except Exception:
            pass
    await page.mouse.wheel(0, delta)
    if container is not None:
        try:
            await container.evaluate("(el, d) => el.scrollTop += d", delta)
        except Exception:
            pass
    else:
        await page.evaluate("d => window.scrollBy(0, d)", delta)


async def human_scroll(
    page: Page,
    container: Optional[Locator] = None,
    total_range: Tuple[int, int] = (640, 720),
    step_range: Tuple[int, int] = (180, 220),
) -> float:
    """Scroll area with wheel + programmatic fallback."""

    total = _rand(total_range)
    remaining = total
    while remaining > 0:
        step = min(_rand(step_range), remaining)
        await _apply_scroll(page, container, step)
        remaining -= step
        await pause((160, 240))
    return total


async def human_type(
    locator: Locator,
    text: str,
    delay_range: Tuple[int, int] = (40, 110),
    typo_chance: float = 0.04,
    corrections: Iterable[str] | None = None,
) -> None:
    """Type text character-by-character with optional typos."""

    if corrections is None:
        corrections = ("й", "ф", "ы", "в")  # simple Cyrillic typo base

    await locator.click()
    await pause((120, 320))
    for index, char in enumerate(text):
        if random.random() < typo_chance:
            typo = random.choice(tuple(corrections))
            await locator.type(typo, delay=_rand(delay_range))
            await pause((80, 160))
            await locator.press("Backspace")
            await pause((40, 120))

        await locator.type(char, delay=_rand(delay_range))
        if index % 3 == 0:
            await pause((30, 90))


async def clear_field(locator: Locator) -> None:
    await locator.click()
    await locator.press("Control+A")
    await locator.press("Backspace")
    await pause((40, 80))


async def fill_field(locator: Locator, text: str, *, enable_typos: bool = False, fast: bool = False) -> None:
    await clear_field(locator)
    if fast:
        await locator.fill(text)
        return
    await human_type(
        locator,
        text,
        typo_chance=0.02 if enable_typos else 0.0,
    )


async def ensure_visible(locator: Locator, timeout_ms: int = 6_000) -> None:
    await locator.wait_for(state="visible", timeout=timeout_ms)
    await locator.scroll_into_view_if_needed()
    await pause((40, 90))


