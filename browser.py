"""Playwright browser management for avtor24 automation."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import AsyncIterator

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

import asyncio

from . import filters, human, logger as logging


PERSISTENT_DIR = Path(".playwright")


def ensure_profile_dir() -> Path:
    PERSISTENT_DIR.mkdir(exist_ok=True)
    return PERSISTENT_DIR


async def start_browser(playwright: Playwright, headless: bool = False) -> BrowserContext:
    """Launch Chromium with a persistent profile and return context."""

    profile = ensure_profile_dir()
    context = await playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile),
        headless=headless,
        viewport={"width": 1340, "height": 900},
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--no-sandbox",
        ],
    )
    return context


@contextlib.asynccontextmanager
async def open_page(headless: bool = False) -> AsyncIterator[Page]:
    """Yield a Playwright page using persistent Chromium context."""

    async with async_playwright() as playwright:
        context = await start_browser(playwright, headless=headless)
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            yield page
        finally:
            await context.close()


async def wait_for_manual_login(page: Page, cfg: "config.BidConfig", timeout: float = 600.0) -> None:
    """Navigate to home and wait until the user logs in manually."""

    await page.goto("https://avtor24.ru/home", wait_until="load")
    menu_orders = page.locator("body > div.w-header > div.menu > div > a.menu__item", has_text="Аукцион заказов")
    try:
        await page.wait_for_selector("body > div.w-header > div.menu", timeout=timeout * 1000)
        await human.human_click(menu_orders)
        await page.wait_for_url("**/order/search", timeout=timeout * 1000)
    except Exception:
        await page.wait_for_url("**/order/search", timeout=timeout * 1000)

    # Apply filters after login
    filter_manager = filters.FilterManager(page, cfg.filter_config)
    filters_applied = await filter_manager.apply_filters_if_needed()

    if not filters_applied:
        logging.warning("⚠️ ВНИМАНИЕ: Фильтры не были применены! Бот продолжит работу без фильтрации.")
        logging.warning("Рекомендуется проверить настройки фильтров в конфигураторе.")
        await asyncio.sleep(3)  # Give user time to see the warning
    else:
        logging.info("Фильтры успешно применены, начинаем работу с фильтрами")


