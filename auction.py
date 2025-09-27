"""Auction scanning and bidding helpers."""

from __future__ import annotations

import asyncio
import heapq
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Deque, Dict, List, Optional, Set, Tuple, TYPE_CHECKING

from playwright.async_api import Locator, Page, TimeoutError

from . import config, human, logger as logging
from .graphql_client import GraphQLClient


class OrderListChangedException(Exception):
    """Raised when the order list has changed (e.g., order was hidden) and search should restart."""
    pass


@dataclass(order=True)
class _QueueEntry:
    """Внутренний элемент очереди с метаданными."""

    priority: int
    sequence: int
    order_id: str = field(compare=False)
    creation_ts: int = field(compare=False)
    payload: Dict = field(compare=False)
    first_seen: float = field(compare=False)
    last_seen: float = field(compare=False)
    stale: bool = field(default=False, compare=False)


class SmartOrderQueue:
    """Умная очередь заказов с приоритетами по времени создания."""

    _COMPACT_FACTOR = 3

    def __init__(self) -> None:
        # Приоритетная куча с ленивым удалением устаревших элементов
        self._heap: List[_QueueEntry] = []
        self._active: Dict[str, _QueueEntry] = {}
        self.processed_ids: Set[str] = set()
        self.known_ids: Set[str] = set()
        self.last_update: float = 0.0
        self._sequence: int = 0

    # region Public API
    def add_orders(self, orders: List[Dict]) -> int:
        """Добавить новые или обновленные заказы в очередь."""

        if not orders:
            return 0

        new_count = 0
        now = time.time()

        for order in orders:
            if not isinstance(order, dict):
                logging.debug("Пропускаем некорректный объект заказа: %r", order)
                continue

            order_id = self._extract_order_id(order)
            if not order_id:
                logging.debug("Пропускаем заказ без ID")
                continue

            if order_id in self.processed_ids:
                logging.debug("Заказ %s уже обработан, пропускаем", order_id)
                continue

            creation_ts = self._extract_creation_ts(order, now)

            if order_id in self._active:
                # Обновляем существующий заказ
                logging.debug("Обновляем данные заказа %s", order_id)
                self._replace_entry(order_id, creation_ts, order, now)
            else:
                logging.debug("Добавляем новый заказ %s", order_id)
                self._add_new_entry(order_id, creation_ts, order, now)
                new_count += 1
                self.known_ids.add(order_id)

        if new_count:
            logging.info("📊 Добавлено %s новых заказов в очередь", new_count)

        self.last_update = now
        self._compact_heap_if_needed()
        return new_count

    def get_next_order(self) -> Optional[Dict]:
        """Получить следующий заказ из очереди (самый свежий)."""

        entry = self._pop_active_entry()
        if not entry:
            logging.debug("📋 Очередь пуста")
            return None

        self.processed_ids.add(entry.order_id)
        logging.info("🎯 Извлекаем заказ %s из очереди", entry.order_id)
        return dict(entry.payload)

    def peek_next_order(self) -> Optional[Dict]:
        """Посмотреть следующий заказ без извлечения из очереди."""

        entry = self._peek_active_entry()
        if entry:
            return dict(entry.payload)
        return None

    def mark_processed(self, order_id: str) -> None:
        """Пометить заказ как обработанный (и удалить из очереди)."""

        if not order_id:
            return

        self.processed_ids.add(order_id)
        entry = self._active.pop(order_id, None)
        if entry:
            entry.stale = True

    def get_queue_size(self) -> int:
        """Получить количество актуальных заказов в очереди."""

        return len(self._active)

    def get_stats(self) -> Dict:
        """Получить статистику очереди."""

        return {
            "queue_size": len(self._active),
            "processed_count": len(self.processed_ids),
            "known_count": len(self.known_ids),
            "last_update": self.last_update,
        }

    def clear_old_orders(self, max_age_hours: int = 24) -> int:
        """Очистить заказы, которые устарели по дате создания."""

        if not self._active:
            return 0

        cutoff = time.time() - max_age_hours * 3600
        removed = 0

        for order_id, entry in list(self._active.items()):
            if entry.creation_ts < cutoff:
                logging.debug("Удаляем устаревший заказ %s", order_id)
                entry.stale = True
                self._active.pop(order_id, None)
                removed += 1

        if removed:
            self._rebuild_heap()

        return removed

    # endregion

    # region Internal helpers
    def _extract_order_id(self, order: Dict) -> Optional[str]:
        order_id = order.get("id") or order.get("order_id")
        if isinstance(order_id, (int, float)):
            return str(int(order_id))
        if isinstance(order_id, str):
            order_id = order_id.strip()
            return order_id or None
        return None

    def _extract_creation_ts(self, order: Dict, fallback: float) -> int:
        raw_value = order.get("creation") or order.get("created_at")

        if isinstance(raw_value, (int, float)):
            timestamp = int(raw_value)
            if timestamp > 0:
                return timestamp

        if isinstance(raw_value, str):
            try:
                timestamp = int(float(raw_value))
                if timestamp > 0:
                    return timestamp
            except ValueError:
                pass

        return int(fallback)

    def _replace_entry(self, order_id: str, creation_ts: int, order: Dict, now: float) -> None:
        existing = self._active.get(order_id)
        if existing:
            existing.stale = True
            merged_payload = dict(existing.payload)
            merged_payload.update(order)
            first_seen = existing.first_seen
        else:
            merged_payload = dict(order)
            first_seen = now

        self._active[order_id] = self._create_entry(order_id, creation_ts, merged_payload, first_seen, now)

    def _add_new_entry(self, order_id: str, creation_ts: int, order: Dict, now: float) -> None:
        payload = dict(order)
        self._active[order_id] = self._create_entry(order_id, creation_ts, payload, now, now)

    def _create_entry(self, order_id: str, creation_ts: int, payload: Dict, first_seen: float, now: float) -> _QueueEntry:
        self._sequence += 1
        entry = _QueueEntry(
            priority=-creation_ts,
            sequence=self._sequence,
            order_id=order_id,
            creation_ts=creation_ts,
            payload=payload,
            first_seen=first_seen,
            last_seen=now,
        )
        heapq.heappush(self._heap, entry)
        return entry

    def _compact_heap_if_needed(self) -> None:
        # Удаляем "мертвые" элементы, если их стало слишком много
        if len(self._heap) <= self._COMPACT_FACTOR * max(1, len(self._active)):
            return

        self._rebuild_heap()

    def _rebuild_heap(self) -> None:
        self._heap = [entry for entry in self._active.values() if not entry.stale]
        heapq.heapify(self._heap)

    def _pop_active_entry(self) -> Optional[_QueueEntry]:
        while self._heap:
            entry = heapq.heappop(self._heap)
            current = self._active.get(entry.order_id)
            if entry.stale or current is not entry:
                continue
            self._active.pop(entry.order_id, None)
            entry.last_seen = time.time()
            return entry
        return None

    def _peek_active_entry(self) -> Optional[_QueueEntry]:
        while self._heap:
            entry = self._heap[0]
            current = self._active.get(entry.order_id)
            if entry.stale or current is not entry:
                heapq.heappop(self._heap)
                continue
            return entry
        return None

    # endregion


class OrderTimeParser:
    """Парсер времени создания заказов из текстового формата."""

    @staticmethod
    def parse_creation_time(time_text: str) -> int:
        """
        Парсер текстового времени в timestamp.

        Поддерживаемые форматы:
        - "Вчера 21:51"
        - "Сегодня 10:30"
        - "2 часа назад"
        - "30 мин назад"
        """
        now = datetime.now()
        time_text = time_text.strip()

        try:
            if "Вчера" in time_text:
                # Вчерашняя дата
                yesterday = now - timedelta(days=1)
                time_str = time_text.replace("Вчера", "").strip()
                time_obj = datetime.strptime(time_str, "%H:%M").time()
                dt = datetime.combine(yesterday.date(), time_obj)

            elif "Сегодня" in time_text:
                # Сегодняшняя дата
                time_str = time_text.replace("Сегодня", "").strip()
                time_obj = datetime.strptime(time_str, "%H:%M").time()
                dt = datetime.combine(now.date(), time_obj)

            elif "назад" in time_text:
                # Относительное время: "2 часа назад", "30 мин назад"
                dt = OrderTimeParser._parse_relative_time(time_text, now)

            else:
                # Попытка парсить как абсолютное время
                try:
                    time_obj = datetime.strptime(time_text, "%H:%M").time()
                    dt = datetime.combine(now.date(), time_obj)
                except ValueError:
                    # Если не удалось, используем текущее время
                    dt = now

            return int(dt.timestamp())

        except (ValueError, AttributeError) as exc:
            logging.debug(f"Ошибка парсинга времени '{time_text}': {exc}")
            return int(now.timestamp())

    @staticmethod
    def _parse_relative_time(time_text: str, now: datetime) -> datetime:
        """Парсер относительного времени."""
        import re

        # Паттерны для поиска чисел и единиц времени
        patterns = [
            (r'(\d+)\s*час', lambda m, dt: dt - timedelta(hours=int(m.group(1)))),
            (r'(\d+)\s*мин', lambda m, dt: dt - timedelta(minutes=int(m.group(1)))),
            (r'(\d+)\s*сек', lambda m, dt: dt - timedelta(seconds=int(m.group(1)))),
        ]

        for pattern, func in patterns:
            match = re.search(pattern, time_text, re.IGNORECASE)
            if match:
                return func(match, now)

        # Если не нашли паттерн, возвращаем текущее время
        return now


class DualBrowserManager:
    """Менеджер для работы с двумя вкладками браузера."""

    def __init__(self, main_page: Page, monitor_page: Page, cfg: config.BidConfig):
        self.main_page = main_page          # Основная вкладка для ставок
        self.monitor_page = monitor_page    # Вкладка для мониторинга новых заказов
        self.cfg = cfg                      # Конфигурация
        self.order_queue = SmartOrderQueue()
        self.monitoring_task = None        # type: Optional[asyncio.Task]
        self.is_monitoring = False

    async def start_monitoring(self) -> None:
        """Запустить параллельный мониторинг первой страницы."""
        if self.is_monitoring:
            return

        self.is_monitoring = True
        self.monitoring_task = asyncio.create_task(self._monitor_loop())
        logging.info("📊 Запущен мониторинг новых заказов в отдельной вкладке")

    async def stop_monitoring(self) -> None:
        """Остановить мониторинг."""
        self.is_monitoring = False
        if self.monitoring_task:
            self.monitoring_task.cancel()
            try:
                await self.monitoring_task
            except asyncio.CancelledError:
                pass
        logging.info("📊 Мониторинг новых заказов остановлен")

    async def _monitor_loop(self) -> None:
        """Основной цикл мониторинга."""
        while self.is_monitoring:
            try:
                # Глубокое сканирование первых страниц для построения очереди
                await self._deep_scan_pages()
                await asyncio.sleep(60)  # Каждую минуту

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logging.error(f"❌ Ошибка в цикле мониторинга: {exc}")
                await asyncio.sleep(30)  # Пауза перед повтором

    async def _deep_scan_pages(self) -> None:
        """Глубокое сканирование первых страниц для построения полной очереди."""
        max_pages_to_scan = self.cfg.smart_queue_pages_to_scan
        total_orders_found = 0
        total_new_orders = 0

        logging.info(f"🔍 Начинаем глубокое сканирование {max_pages_to_scan} страниц для построения умной очереди...")

        try:
            # Проверить текущую страницу
            current_url = self.monitor_page.url
            logging.info(f"📍 Текущая страница мониторинга: {current_url}")

            # Начать с первой страницы
            logging.info("📄 Переходим на первую страницу...")
            page_change_success = await self._go_to_page(self.monitor_page, 1)
            if not page_change_success:
                logging.warning("⚠️ Не удалось перейти на первую страницу, пробуем работать с текущей")
            await asyncio.sleep(3)  # Дать странице загрузиться

            # Проверить, что мы на странице заказов
            current_url = self.monitor_page.url
            if "order/search" not in current_url:
                logging.error(f"❌ Мы не на странице заказов! URL: {current_url}")
                # Попробовать перейти на страницу заказов напрямую
                logging.info("🔄 Пробуем перейти на страницу заказов напрямую...")
                await self.monitor_page.goto("https://avtor24.ru/order/search", wait_until="networkidle")
                await asyncio.sleep(3)

            for page_num in range(1, max_pages_to_scan + 1):
                logging.info(f"📄 Сканируем страницу {page_num}...")

                # Сканировать заказы на текущей странице
                orders = await self._scan_orders_on_page(self.monitor_page)
                page_orders_count = len(orders)
                total_orders_found += page_orders_count

                logging.info(f"📄 Страница {page_num}: найдено {page_orders_count} заказов")

                # Показать первые несколько заказов для отладки
                if orders:
                    for i, order in enumerate(orders[:3]):
                        logging.info(f"   • Заказ {i+1}: ID {order.get('id')} - {order.get('title', '')[:50]}")

                # Добавить в очередь
                new_count = self.order_queue.add_orders(orders)
                total_new_orders += new_count

                # Если на странице меньше 10 заказов, значит это последняя страница
                if page_orders_count < 10:
                    logging.info(f"📄 Страница {page_num} содержит только {page_orders_count} заказов - это последняя страница")
                    break

                # Перейти на следующую страницу
                if page_num < max_pages_to_scan:
                    logging.info(f"📄 Переходим на страницу {page_num + 1}...")
                    success = await self._go_to_page(self.monitor_page, page_num + 1)
                    if not success:
                        logging.warning(f"⚠️ Не удалось перейти на страницу {page_num + 1}, останавливаемся")
                        break
                    await asyncio.sleep(2)  # Дать странице загрузиться

            # Вернуться на первую страницу для следующего цикла
            logging.info("📄 Возвращаемся на первую страницу...")
            await self._go_to_page(self.monitor_page, 1)

            # Статистика
            stats = self.order_queue.get_stats()
            logging.info(f"🔍 Глубокое сканирование завершено: найдено {total_orders_found} заказов, добавлено {total_new_orders} новых. Очередь: {stats['queue_size']} заказов")

        except Exception as exc:
            logging.error(f"❌ Ошибка глубокого сканирования: {exc}")
            import traceback
            logging.debug(f"Traceback: {traceback.format_exc()}")

    async def _go_to_page(self, page: Page, page_num: int) -> bool:
        """Перейти на указанную страницу пагинации."""
        try:
            logging.info(f"🔍 Ищем кнопку страницы {page_num}...")

            # Найти контейнер пагинации
            pagination = page.locator(PAGINATION_LIST)
            pagination_count = await pagination.count()
            logging.info(f"🔍 Найдено {pagination_count} контейнеров пагинации")

            if pagination_count == 0:
                # Попробовать альтернативные селекторы пагинации
                alt_pagination_selectors = [
                    "div[class*='pagination']",
                    "nav[class*='pagination']",
                    ".pagination",
                    "[class*='page']",
                    "div[class*='list']"
                ]

                for alt_selector in alt_pagination_selectors:
                    alt_pagination = page.locator(alt_selector)
                    alt_count = await alt_pagination.count()
                    if alt_count > 0:
                        logging.info(f"🔍 Найден альтернативный контейнер пагинации: {alt_selector}")
                        pagination = alt_pagination
                        pagination_count = alt_count
                        break

            if pagination_count == 0:
                logging.warning("❌ Контейнер пагинации не найден")

                # Для отладки покажем структуру страницы
                try:
                    body_html = await page.locator("body").inner_html()
                    # Найдем возможные элементы пагинации в HTML
                    if "pagination" in body_html.lower() or "page" in body_html.lower():
                        logging.info("🔍 В HTML найден текст связанный с пагинацией")
                        # Можно показать кусок HTML для анализа
                        lines = body_html.split('\n')
                        pagination_lines = [line for line in lines if 'pagi' in line.lower() or 'page' in line.lower()]
                        if pagination_lines:
                            logging.info("📄 Возможные элементы пагинации в HTML:")
                            for line in pagination_lines[:5]:
                                logging.info(f"   {line.strip()[:100]}...")
                except Exception as html_exc:
                    logging.debug(f"Не удалось проанализировать HTML: {html_exc}")

                return False

            # Найти кнопки страниц
            page_buttons = page.locator(PAGINATION_ITEM)
            buttons_count = await page_buttons.count()
            logging.info(f"🔍 Найдено {buttons_count} кнопок страниц")

            if buttons_count == 0:
                # Попробовать более общие селекторы
                alt_button_selectors = [
                    f"{PAGINATION_LIST} span",
                    f"{PAGINATION_LIST} div",
                    "span[class*='page']",
                    "div[class*='item']",
                    "[role='button']",
                    "button"
                ]

                for alt_selector in alt_button_selectors:
                    alt_buttons = page.locator(alt_selector)
                    alt_count = await alt_buttons.count()
                    if alt_count > 0:
                        logging.info(f"🔍 Найдено {alt_count} кнопок с селектором: {alt_selector}")
                        page_buttons = alt_buttons
                        buttons_count = alt_count
                        break

            # Показать все найденные кнопки для отладки
            if buttons_count > 0:
                logging.info("📋 Список всех найденных кнопок страниц:")
                for i in range(min(buttons_count, 10)):  # Не больше 10 для логов
                    button = page_buttons.nth(i)
                    text = await button.text_content()
                    is_visible = await button.is_visible()
                    logging.info(f"   {i+1}. '{text}' (visible: {is_visible})")

            # Найти нужную кнопку
            for i in range(buttons_count):
                button = page_buttons.nth(i)
                button_text = await button.text_content()

                try:
                    button_page_num = int(button_text.strip())
                    if button_page_num == page_num:
                        # Нашли нужную страницу
                        is_enabled = await button.is_enabled()
                        is_visible = await button.is_visible()

                        logging.info(f"✅ Найдена кнопка страницы {page_num} (enabled: {is_enabled}, visible: {is_visible})")

                        if is_enabled and is_visible:
                            logging.info(f"🔘 Нажимаем кнопку страницы {page_num}")
                            await button.click()
                            await page.wait_for_load_state("networkidle")
                            await asyncio.sleep(2)  # Увеличим паузу
                            logging.info(f"✅ Перешли на страницу {page_num}")
                            return True
                        else:
                            logging.warning(f"❌ Кнопка страницы {page_num} неактивна или скрыта")
                            return False
                except ValueError:
                    # Это не числовая кнопка (например, "...")
                    continue

            logging.warning(f"❌ Кнопка страницы {page_num} не найдена среди {buttons_count} кнопок")
            return False

        except Exception as exc:
            logging.error(f"❌ Ошибка перехода на страницу {page_num}: {exc}")
            return False

    async def _scan_first_page(self) -> None:
        """Сканировать первую страницу на новые заказы."""
        try:
            logging.debug("🔄 Начинаем сканирование первой страницы...")

            # Перейти на первую страницу
            current_url = self.monitor_page.url
            if not current_url or "order/search" not in current_url:
                logging.debug("📄 Переходим на страницу поиска заказов...")
                await self.monitor_page.goto("https://avtor24.ru/order/search", wait_until="networkidle")
                await asyncio.sleep(2)  # Дать странице полностью загрузиться
                logging.debug("✅ Перешли на страницу заказов")

            # Проверить, что мы на правильной странице
            current_url = self.monitor_page.url
            if "order/search" not in current_url:
                logging.error(f"❌ Мы не на странице заказов, текущий URL: {current_url}")
                return

            # Дождаться загрузки заказов
            logging.debug("⏳ Ждем загрузки заказов...")
            loaded = await self._wait_for_orders_loaded(self.monitor_page)
            if not loaded:
                logging.warning("⚠️ Не удалось дождаться загрузки заказов")
                return

            # Сканировать заказы на странице
            logging.debug("🔍 Сканируем заказы на странице...")
            orders = await self._scan_orders_on_page(self.monitor_page)

            logging.info(f"📊 Найдено {len(orders)} заказов на странице")

            # Добавить в очередь
            new_count = self.order_queue.add_orders(orders)

            if new_count > 0:
                stats = self.order_queue.get_stats()
                logging.info(f"📋 Добавлено {new_count} новых заказов. Очередь: {stats['queue_size']} заказов")
            else:
                logging.debug("📋 Новых заказов не найдено")

        except Exception as exc:
            logging.error(f"❌ Ошибка сканирования первой страницы: {exc}")
            import traceback
            logging.debug(f"Traceback: {traceback.format_exc()}")

    async def _wait_for_orders_loaded(self, page: Page, timeout: float = 10.0) -> bool:
        """Дождаться загрузки заказов на странице."""
        try:
            # Ждем появления контейнера заказов
            await page.locator(ORDER_LIST).wait_for(state="visible", timeout=timeout * 1000)
            await asyncio.sleep(0.5)  # Дополнительная пауза

            # Проверить, что есть карточки заказов
            cards = page.locator(ORDER_CARD)
            await cards.first.wait_for(state="attached", timeout=5000)

            return True

        except TimeoutError:
            logging.warning("⏳ Не дождались загрузки заказов на странице мониторинга")
            return False

    async def _scan_orders_on_page(self, page: Page) -> List[Dict]:
        """Сканировать заказы на текущей странице."""
        orders = []

        try:
            # Проверить, что ORDER_LIST существует
            order_list = page.locator(ORDER_LIST)
            order_list_count = await order_list.count()
            logging.info(f"🔍 Проверяем ORDER_LIST: найдено {order_list_count} элементов")

            if order_list_count == 0:
                logging.warning("❌ Контейнер заказов ORDER_LIST не найден")

                # Попробовать найти другие возможные контейнеры
                possible_containers = [
                    "div[class*='order'][class*='list']",
                    "div[class*='auction'][class*='list']",
                    ".orders-container",
                    "#orders-list",
                    "main div[class*='list']"
                ]

                for container_selector in possible_containers:
                    containers = page.locator(container_selector)
                    container_count = await containers.count()
                    if container_count > 0:
                        logging.info(f"🔍 Найден альтернативный контейнер: {container_selector} ({container_count})")
                        # Можно использовать этот контейнер вместо ORDER_LIST
                        break

                return orders

            cards = page.locator(ORDER_CARD)
            card_count = await cards.count()

            logging.info(f"🔍 Найдено {card_count} карточек заказов (ORDER_CARD)")

            if card_count == 0:
                logging.warning("⚠️ Основной селектор ORDER_CARD не нашел карточек")

                # Попробовать альтернативные селекторы
                alt_selectors = [
                    "div[data-testid='order-card']",
                    ".order-card",
                    "div.auctionOrder",
                    "[class*='order'][class*='card']",
                    "div[class*='card']",
                    "article",
                    "div[class*='order']"
                ]

                for alt_selector in alt_selectors:
                    alt_cards = page.locator(alt_selector)
                    alt_count = await alt_cards.count()
                    if alt_count > 0:
                        logging.info(f"✅ Найдено {alt_count} карточек с селектором: {alt_selector}")
                        cards = alt_cards
                        card_count = alt_count

                        # Проверим, что это действительно карточки заказов (должны содержать ссылки)
                        valid_cards = 0
                        for i in range(min(5, alt_count)):
                            card = alt_cards.nth(i)
                            links = card.locator("a[href*='/order/']")
                            if await links.count() > 0:
                                valid_cards += 1

                        if valid_cards > 0:
                            logging.info(f"✅ Из них {valid_cards} содержат ссылки на заказы - используем этот селектор")
                            break
                        else:
                            logging.info(f"❌ Селектор {alt_selector} не содержит ссылок на заказы")

            if card_count == 0:
                logging.error("🚨 КРИТИЧНО: Не найдено ни одной карточки заказов на странице!")

                # Для отладки покажем структуру страницы
                try:
                    body_html = await page.inner_html()
                    # Найдем элементы, которые могут быть карточками
                    if "order" in body_html.lower():
                        logging.info("🔍 В HTML найден текст 'order'")
                        lines = body_html.split('\n')
                        order_lines = [line for line in lines if 'order' in line.lower() and ('href' in line.lower() or 'card' in line.lower())]
                        if order_lines:
                            logging.info("📄 Возможные элементы заказов в HTML:")
                            for line in order_lines[:10]:  # Покажем первые 10
                                logging.info(f"   {line.strip()[:150]}...")

                    # Проверим, есть ли вообще контент на странице
                    if len(body_html) < 1000:
                        logging.warning(f"⚠️ Страница слишком маленькая ({len(body_html)} символов) - возможно, не загрузилась")
                        logging.info(f"📄 Содержимое страницы: {body_html[:500]}...")

                except Exception as html_exc:
                    logging.debug(f"Не удалось проанализировать HTML страницы: {html_exc}")

                return orders

            # Ограничение для производительности - сканируем максимум 20 заказов
            max_orders = min(card_count, 20)

            logging.info(f"🔄 Начинаем извлечение данных из {max_orders} карточек...")

            for i in range(max_orders):
                try:
                    logging.info(f"🔍 Обрабатываем карточку {i+1}/{max_orders}...")
                    card = cards.nth(i)
                    order_data = await self._extract_order_data(card)

                    if order_data:
                        orders.append(order_data)
                        logging.info(f"✅ Извлечен заказ {order_data['id']}: {order_data['title'][:30]}...")
                    else:
                        logging.info(f"❌ Не удалось извлечь данные заказа {i+1}")

                except Exception as exc:
                    logging.info(f"❌ Ошибка извлечения данных заказа {i+1}: {exc}")
                    continue

            logging.info(f"📊 Итого извлечено {len(orders)} заказов из {card_count} карточек")

        except Exception as exc:
            logging.error(f"❌ Ошибка сканирования страницы: {exc}")
            import traceback
            logging.debug(f"Traceback: {traceback.format_exc()}")

        return orders

    async def _extract_order_data(self, card: Locator) -> Optional[Dict]:
        """Извлечь данные заказа из карточки."""
        try:
            # Получить ID заказа из ссылки
            link = card.locator(CARD_TITLE_ANCHOR).first
            href = await link.get_attribute("href", timeout=2000)

            if not href:
                logging.info("❌ Ссылка на заказ не найдена")
                return None

            # Извлечь ID из URL
            match = re.search(r'/order/(?:getoneorder/)?(\d+)', href)
            if not match:
                logging.info(f"❌ Не удалось извлечь ID из URL: {href}")
                return None

            order_id = match.group(1)
            logging.info(f"🔗 Найден заказ ID: {order_id}")

            # Получить время создания
            time_element = card.locator(".orderCreation")
            time_text = await time_element.text_content(timeout=2000)

            if time_text:
                timestamp = OrderTimeParser.parse_creation_time(time_text)
                logging.info(f"🕐 Время создания: '{time_text}' -> {timestamp}")
            else:
                timestamp = int(datetime.now().timestamp())
                logging.info("🕐 Время создания не найдено, использую текущее время")

            # Получить заголовок - пробуем разные селекторы
            title = "Без названия"

            # Попробовать различные селекторы заголовков
            title_selectors = [
                "h3",
                ".orderTitle",
                "[class*='title']",
                "[class*='order'][class*='title']",
                "a[href*='/order/'] span",
                "a[href*='/order/']",
                "[data-testid*='title']",
                ".card-title",
                ".order-name"
            ]

            for selector in title_selectors:
                try:
                    title_element = card.locator(selector).first
                    candidate_title = await title_element.text_content(timeout=1000)
                    if candidate_title and len(candidate_title.strip()) > 3:
                        title = candidate_title.strip()
                        logging.info(f"✅ Заголовок найден селектором '{selector}': {title[:50]}...")
                        break
                except Exception:
                    continue

            if title == "Без названия":
                logging.info("⚠️ Заголовок не найден ни одним селектором")
            logging.info(f"📝 Заголовок: '{title.strip()}'")

            result = {
                'id': order_id,
                'creation': timestamp,
                'title': title.strip(),
                'url': f"https://avtor24.ru{href}"
            }

            logging.info(f"✅ Успешно извлечены данные заказа: {result}")
            return result

        except Exception as exc:
            logging.info(f"❌ Критическая ошибка извлечения данных заказа: {exc}")
            import traceback
            logging.info(f"Traceback: {traceback.format_exc()}")
            return None

    def get_next_order(self) -> Optional[Dict]:
        """Получить следующий заказ из очереди."""
        return self.order_queue.get_next_order()

    def get_queue_stats(self) -> Dict:
        """Получить статистику очереди."""
        return self.order_queue.get_stats()


if TYPE_CHECKING:
    from .message_scheduler import MessageScheduler


ORDER_LIST = "div.styled__OrderListStyled-sc-anwyvn-0"
ORDER_CARD = f"{ORDER_LIST} div.styled__Container-sc-bhkcjd-0"
CARD_TITLE_ANCHOR = "a[href*='/order/']"
PAGINATION_LIST = "div.styled__List-sc-10myatr-1"
PAGINATION_ITEM = f"{PAGINATION_LIST} div.styled__Item-sc-10myatr-2"
PAGINATION_ACTIVE = "ePkAKA"  # Класс активной страницы
PAGINATION_PAGE = "fcnjeo"    # Класс обычной страницы
ORDER_HIDE_BUTTON = "button.auctionOrder_hideBtn"
MY_BID_BADGE = "div.orderOfferBid, div:has-text('Моя ставка'), div:has-text('Заказчик не заинтересован вашей ставкой')"
MAKE_OFFER_BUTTON = "div.styled__ActionsStyled-sc-do1gbv-0 button:has(.uikit-button_label:has-text('Поставить ставку'))"
MODAL = "div.styled__AuctionOrderModalStyled-sc-172cjna-0"
PRICE_INPUT = "#MakeOffer__inputBid"
COMMENT_INPUT = "#makeOffer_comment"
SUBMIT_BUTTON = "button:has(.uikit-button_label:has-text('Поставить ставку'))"
CAPTCHA_FRAME = "iframe[src*='smartcaptcha']"
DISMISSED_PANEL = "div.styled__DismissedInfoStyled-sc-133qlln-0"


@dataclass
class OrderStub:
    title: str | None = None
    work_type: str | None = None
    subject: str | None = None


async def ensure_orders_loaded(page: Page, timeout_ms: int = 10000) -> bool:
    loader = page.locator("div[role='progressbar']")
    try:
        await loader.wait_for(state="hidden", timeout=timeout_ms)
    except TimeoutError:
        logging.warning("Список заказов не успел скрыть спиннер — продолжаем с тем, что есть.")

    cards = page.locator(ORDER_CARD)
    try:
        await cards.first.wait_for(state="attached", timeout=timeout_ms)
        return True
    except TimeoutError:
        if await cards.count() == 0:
            logging.warning("На странице нет карточек заказов (возможно, фильтр пустой).")
        else:
            logging.warning("Карточки нашлись в DOM, но остаются невидимыми.")
        return False


async def highlight(card: Locator, duration: float = 0.4) -> None:
    try:
        await card.evaluate(
            "(node) => { node.dataset.prevShadow = node.style.boxShadow; node.style.boxShadow = '0 0 0 2px rgba(87, 121, 255, 0.8)'; }"
        )
        await asyncio.sleep(duration)
        await card.evaluate(
            "(node) => { node.style.boxShadow = node.dataset.prevShadow || ''; delete node.dataset.prevShadow; }"
        )
    except Exception:
        pass


async def scroll_and_scan(page: Page, cfg: config.BidConfig, tracker: "OrderTracker", trigger: "HybridTrigger") -> Optional[Tuple[Locator, int]]:
    await force_close_modal(page, page.locator(MODAL), aggressive=True, reason="Перед сканированием закрываем подвисшую модалку.")
    cards = page.locator(ORDER_CARD)
    loaded = await ensure_orders_loaded(page)
    if not loaded:
        logging.warning("Не дождались загрузки заказов.")
        return None

    tracker.begin_cycle()

    for order_hint in trigger.iter_priority_hints():
        result = await find_first_available_button(cards, tracker, prefer_top=True, order_hint=order_hint)
        if result:
            button, card_index = result
            return (button, card_index)

    fresh_result = await find_first_available_button(cards, tracker, prefer_top=True)
    if fresh_result:
        button, card_index = fresh_result
        return (button, card_index)

    # If no fresh button found, try to find any button (not just prefer_top)
    # This helps when top orders are processed recently
    any_result = await find_first_available_button(cards, tracker, prefer_top=False)
    if any_result:
        button, card_index = any_result
        logging.info("Найдена кнопка не из топа (верхние заказы обработаны недавно)")
        return (button, card_index)

    scroll_container = page.locator(ORDER_LIST)

    for attempt in range(cfg.max_scrolls):
        tracker.begin_cycle()
        result = await find_first_available_button(cards, tracker)
        if result:
            button, card_index = result
            return (button, card_index)

        # Also try without prefer_top constraint
        any_result = await find_first_available_button(cards, tracker, prefer_top=False)
        if any_result:
            button, card_index = any_result
            logging.info(f"Найдена кнопка после скролла (попытка {attempt + 1})")
            return (button, card_index)
        logging.info(f"Кнопок нет, скроллим попытка {attempt + 1}...")
        distance = await human.human_scroll(
            page,
            container=scroll_container,
            total_range=cfg.scroll_step_range,
            step_range=(200, 240),
        )
        logging.info(f"Скроллим вниз на ~{int(distance)}px")
        await human.pause((250, 420))

    logging.warning("Пролистали ленту, но кнопок ставок не нашли.")
    recovery = await human.human_scroll(
        page,
        container=scroll_container,
        total_range=cfg.scroll_recovery_range,
        step_range=(180, 220),
    )
    logging.info(f"Возвратный скролл на ~{int(recovery)}px")
    return None


async def find_first_available_button(cards: Locator, tracker: "OrderTracker", prefer_top: bool = False, order_hint: Optional[str] = None) -> Optional[Tuple[Locator, int]]:
    count = await cards.count()
    if prefer_top:
        count = min(count, tracker.prefer_top_window)

    for idx in range(count):
        # CRITICAL: Skip cards that were already processed in this session (by index)
        if idx in tracker.processed_card_indices:
            logging.debug(f"Карточка {idx}: ПРОПУСК - обработана ранее (по индексу)")
            continue

        card = cards.nth(idx)
        order_id = await tracker.extract_order_id(card)

        # Debug logging for first few cards
        if idx < 3:
            logging.debug(f"Карточка {idx}: order_id={order_id}, processed_recently={order_id in tracker.processed_recently if order_id else 'N/A'}")

        if order_hint and order_id != order_hint:
            continue
        if order_id and not tracker.can_attempt(order_id):
            logging.debug(f"Карточка {idx} ({order_id}): пропускаем - can_attempt=False")
            continue
        # Skip orders that were already processed in this cycle (even if not exhausted)
        if order_id and order_id in tracker.processed_this_cycle:
            logging.debug(f"Карточка {idx} ({order_id}): пропускаем - processed_this_cycle")
            continue

        # Skip orders that were processed recently (prevents loops across attempts)
        if order_id and order_id in tracker.processed_recently:
            logging.debug(f"Карточка {idx} ({order_id}): пропускаем - processed_recently")
            continue

        if await card.locator(MY_BID_BADGE).count():
            tracker.mark_exhausted(order_id)
            continue

        offer_buttons = card.locator(MAKE_OFFER_BUTTON)
        if await offer_buttons.count():
            btn = offer_buttons.first
            if await btn.is_enabled():
                try:
                    await card.hover()
                except Exception:
                    pass
                await highlight(card)
                tracker.mark_seen(order_id)
                return (btn, idx)
    return None


async def extract_order_info(modal: Locator) -> OrderStub:
    title_locator = modal.locator("div.auctionOrderTitle")
    if not await title_locator.count():
        title_locator = modal.locator("h1, h2, h3").first
    try:
        raw_title = await title_locator.first.text_content(timeout=5_000)
    except TimeoutError:
        raw_title = ""

    def field_value(label: str) -> Locator:
        return modal.locator(
            f"div.styled__FieldStyled-sc-12kp2q-1:has(div:text('{label}')) div[data-has-value='true']"
        )

    work_type_loc = field_value("Тип работы")
    subject_loc = field_value("Предмет")

    work_type = (await work_type_loc.first.text_content()) if await work_type_loc.count() else None
    subject = (await subject_loc.first.text_content()) if await subject_loc.count() else None

    return OrderStub(title=raw_title.strip(), work_type=work_type, subject=subject)


async def place_bid(page: Page, cfg: config.BidConfig, message_scheduler: Optional["MessageScheduler"] = None, graphql_client: Optional[GraphQLClient] = None) -> bool:
    """Place a bid on an auction order."""
    tracker = OrderTracker(cfg=cfg)
    trigger = getattr(page, "_bot_hybrid_trigger", None)
    if trigger:
        trigger.update_tracker(tracker)
    else:
        trigger = HybridTrigger(page, tracker)
        await trigger.start()
        setattr(page, "_bot_hybrid_trigger", trigger)

    result = await scroll_and_scan(page, cfg, tracker, trigger)
    if not result:
        logging.warning("Не нашли подходящую карточку для ставки.")
        return False

    button, card_index = result
    tracker.current_card_index = card_index

    workflow = BidWorkflow(page=page, trigger_button=button, cfg=cfg, tracker=tracker, trigger=trigger, message_scheduler=message_scheduler, graphql_client=graphql_client, card_index=card_index)
    try:
        return await workflow.run()
    except OrderListChangedException as exc:
        logging.info(f"📋 Список заказов изменился ({exc}), начинаем поиск заново")
        # List has changed, return False to restart the search cycle
        return False


class BidWorkflow:
    def __init__(self, page: Page, trigger_button: Locator, cfg: config.BidConfig, tracker: "OrderTracker", trigger: "HybridTrigger", message_scheduler: Optional["MessageScheduler"] = None, graphql_client: Optional[GraphQLClient] = None, card_index: Optional[int] = None) -> None:
        self.page = page
        self.trigger_button = trigger_button
        self.cfg = cfg
        self.modal = page.locator(MODAL)
        self.submission_done = False
        self.tracker = tracker
        self.trigger = trigger
        self.current_order_id: str | None = tracker.current_order
        self.message_scheduler = message_scheduler
        self.graphql_client = graphql_client
        self.card_index = card_index

    async def _extract_order_id_from_card(self) -> str | None:
        """Extract order ID from the card containing the trigger button."""
        try:
            # Find the card that contains this button
            card = self.trigger_button.locator("xpath=ancestor::div[contains(@class, 'styled__Container')]").first

            if await card.count() == 0:
                # Try alternative selector
                card = self.trigger_button.locator("xpath=ancestor::div[contains(@class, 'OrderCard')]").first

            if await card.count() > 0:
                # Use more specific selector to avoid multiple matches - prefer title link
                title_link = card.locator("a[data-discover='true'][href*='/order/']").first
                if await title_link.count() > 0:
                    href = await title_link.get_attribute("href")
                    if href:
                        import re
                        match = re.search(r'/order/(?:getoneorder/)?(\d+)', href)
                        if match:
                            return match.group(1)

                # Fallback: general anchor selector
                href = await card.locator(CARD_TITLE_ANCHOR).first.get_attribute("href")
                if href:
                    import re
                    match = re.search(r'/order/(?:getoneorder/)?(\d+)', href)
                    if match:
                        return match.group(1)

                # Fallback: data-id attribute
                data_id = await card.get_attribute("data-id")
                if data_id and data_id.isdigit():
                    return data_id

        except Exception as exc:
            logging.warning(f"Не удалось извлечь order_id из карточки: {exc}")

        return None

    async def _get_order_id_from_modal(self) -> str | None:
        """Extract order ID from modal URL or content (fallback method)."""
        try:
            # Try to get order ID from URL (most reliable)
            url = self.page.url
            if "/order/" in url:
                # Extract from URL like /order/12345 or /order/getoneorder/12345
                import re
                match = re.search(r'/order/(?:getoneorder/)?(\d+)', url)
                if match:
                    return match.group(1)

            # Fallback 1: try to extract from modal content links
            order_links = self.modal.locator("a[href*='/order/']")
            count = await order_links.count()
            for i in range(count):
                href = await order_links.nth(i).get_attribute("href")
                if href:
                    match = re.search(r'/order/(?:getoneorder/)?(\d+)', href)
                    if match:
                        return match.group(1)

            # Fallback 2: try to extract from page title or meta
            title_element = self.page.locator("title")
            if await title_element.count() > 0:
                title_text = await title_element.text_content()
                if title_text and "Заказ" in title_text:
                    # Try to extract number from title like "Заказ №12345"
                    import re
                    match = re.search(r'Заказ\s*[№#]?\s*(\d+)', title_text)
                    if match:
                        return match.group(1)

            # Fallback 3: try to extract from any element containing order number
            order_number_elements = self.page.locator("text=/\\d{5,}/")  # Look for 5+ digit numbers
            count = await order_number_elements.count()
            for i in range(min(count, 5)):  # Check first 5 matches
                text = await order_number_elements.nth(i).text_content()
                if text and len(text.strip()) >= 5 and text.strip().isdigit():
                    # Additional validation - should be reasonable order ID range
                    order_num = int(text.strip())
                    if 10000 <= order_num <= 999999:  # Reasonable order ID range
                        return text.strip()

        except Exception as exc:
            logging.debug(f"Error extracting order_id from modal: {exc}")
        return None

    async def _hide_rejected_order(self) -> None:
        """Hide the rejected order from the auction list by clicking the hide button."""
        try:
            if self.card_index is None:
                logging.debug("Cannot hide order: card_index is None")
                return

            # IMPORTANT: Close the modal first to avoid conflicts
            logging.debug(f"Закрываем модальное окно перед скрытием заказа {self.current_order_id}")
            await self._fallback_close()
            await asyncio.sleep(0.3)  # Give modal time to close completely

            # Find the card by index
            cards = self.page.locator(ORDER_CARD)
            if self.card_index >= await cards.count():
                logging.debug(f"Cannot hide order: card index {self.card_index} out of range")
                return

            card = cards.nth(self.card_index)

            # Find the hide button on this card
            hide_button = card.locator("button.auctionOrder_hideBtn")

            if await hide_button.count() > 0:
                logging.info(f"Нажимаем кнопку скрытия заказа {self.current_order_id}")
                await hide_button.click()
                await asyncio.sleep(0.5)  # Wait for hide action to complete
                logging.info(f"✅ Заказ {self.current_order_id} скрыт из списка")
            else:
                logging.debug(f"Кнопка скрытия не найдена для заказа {self.current_order_id}")

        except Exception as exc:
            logging.debug(f"Ошибка при скрытии заказа {self.current_order_id}: {exc}")

    async def _ensure_order_id_extracted(self) -> bool:
        """Ensure order_id is extracted before any other operations. Returns False if extraction fails."""
        max_attempts = 3
        attempt_delay = 0.3

        for attempt in range(max_attempts):
            logging.debug(f"🔍 Извлечение order_id, попытка {attempt + 1}/{max_attempts}")

            # Try from card first
            if not self.current_order_id:
                self.current_order_id = await self._extract_order_id_from_card()
                if self.current_order_id:
                    logging.debug(f"✅ Order_id извлечен из карточки: {self.current_order_id}")
                    return True

            # Try from modal as fallback
            if not self.current_order_id:
                self.current_order_id = await self._get_order_id_from_modal()
                if self.current_order_id:
                    logging.debug(f"✅ Order_id извлечен из модального окна: {self.current_order_id}")
                    return True

            if attempt < max_attempts - 1:
                logging.debug(f"⏳ Order_id не найден, ждем {attempt_delay}с перед следующей попыткой...")
                await asyncio.sleep(attempt_delay)

        # If we get here, all attempts failed
        logging.error("❌ CRITICAL: Не удалось извлечь order_id после всех попыток")
        logging.error("Это может привести к неправильному отслеживанию заказов и зацикливанию")
        logging.error("Рекомендуется проверить селекторы или структуру страницы")

        # Mark this as a failed attempt - we can't safely continue without knowing the order_id
        return False

    async def run(self) -> bool:
        try:
            await close_overlays(self.page)
            if not await self._open_modal():
                return False

            # CRITICAL: Extract order ID before any other operations
            # If we can't identify the order, we can't safely continue
            if not await self._ensure_order_id_extracted():
                logging.warning("⚠️ Пропускаем заказ - не удалось извлечь order_id")
                return False

            # Additional safety check: if this order was already processed in current cycle, skip it
            if self.current_order_id in self.tracker.processed_this_cycle:
                logging.info(f"⏭️ Заказ {self.current_order_id} уже обработан в этом цикле, пропускаем")
                return False

            # Now we have a confirmed order_id, extract order info
            order = await extract_order_info(self.modal)

            # Check if order was already rejected by customer
            rejected_selectors = [
                "text=К сожалению, заказчика не заинтересовала Ваша ставка",
                "text=заказчика не заинтересовала Ваша ставка",
                "text=Заказчик не заинтересован вашей ставкой"
            ]

            for selector in rejected_selectors:
                rejected_text = self.modal.locator(selector)
                if await rejected_text.count() > 0:
                    logging.warning(f"⚠️ Заказ {self.current_order_id} уже отклонен заказчиком, скрываем заказ")
                    # Try to hide the rejected order from the list
                    await self._hide_rejected_order()
                    # Mark as exhausted and processed
                    self.tracker.mark_exhausted(self.current_order_id)
                    # List has changed, need to restart search
                    raise OrderListChangedException(f"Order {self.current_order_id} was hidden from list")

            # Wait for modal to fully load and check if bid input field is available
            # Order might not be available for bidding or modal is still loading
            bid_input = self.modal.locator("#MakeOffer__inputBid")

            # Give modal more time to load - check multiple times with delays
            max_checks = 5
            check_interval = 0.5  # 500ms between checks

            for attempt in range(max_checks):
                if await bid_input.count() > 0:
                    # Additional check - ensure input is actually visible and enabled
                    try:
                        is_visible = await bid_input.first.is_visible()
                        if is_visible:
                            break
                    except Exception:
                        pass

                if attempt < max_checks - 1:  # Don't sleep on last attempt
                    logging.info(f"⏳ Ждем загрузки модального окна (попытка {attempt + 1}/{max_checks})...")
                    await asyncio.sleep(check_interval)
                else:
                    logging.warning(f"⚠️ Заказ {self.current_order_id} не доступен для ставок (нет поля ввода после {max_checks} проверок), скрываем заказ")
                    # Try to hide the unavailable order from the list
                    await self._hide_rejected_order()
                    # Mark as exhausted and processed
                    self.tracker.mark_exhausted(self.current_order_id)
                    # List has changed, need to restart search
                    raise OrderListChangedException(f"Order {self.current_order_id} was hidden from list")

            message = self.cfg.build_message(order.title, order.work_type, order.subject)

            if not await self._prepare_fields(message):
                return False

            if not await self._click_submit():
                return False

            logging.success(f"Ставка отправлена по заказу: {order.title}")
            await self.trigger.notify_success(order_id=self.current_order_id)

            # Close modal immediately after successful bid to return to order list
            await force_close_modal(self.page, self.modal, aggressive=True, reason="Закрываем модалку после успешной ставки")
            await asyncio.sleep(0.3)  # Brief pause for modal to close

            # After successful bid, get customer_id and schedule follow-up message
            if self.message_scheduler and self.current_order_id and self.graphql_client:
                try:
                    # Get customer_id after successful bid (now it should be available)
                    customer_id = await self.graphql_client.get_customer_id(self.current_order_id)

                    if customer_id:
                        # Schedule follow-up message with real customer_id
                        success = self.message_scheduler.schedule_followup_message(
                            order_id=self.current_order_id,
                            customer_id=customer_id,
                            order_title=order.title or "заказ",
                            subject_name=order.subject or "предмет",
                            work_type_name=order.work_type or "работа",
                            delay_minutes=self.cfg.followup_delay_minutes
                        )
                        if success:
                            logging.info(f"📅 Запланировано сообщение для заказа {self.current_order_id} (customer_id: {customer_id})")
                        else:
                            logging.warning(f"⚠️ Не удалось запланировать сообщение для {self.current_order_id}")
                    else:
                        logging.warning(f"⚠️ Customer_id не найден для заказа {self.current_order_id} даже после ставки")
                except Exception as exc:
                    logging.error(f"❌ Ошибка планирования: {exc}")

            await self._post_success_scroll()
            return True
        finally:
            if not self.submission_done:
                await self._fallback_close()
            await force_close_modal(self.page, self.modal, aggressive=True, reason="Страховка после завершения воркфлоу.")
            self.tracker.mark_attempt_completed(success=self.submission_done, order_id=self.current_order_id)

    async def _run_step(self, name: str, coro) -> bool:
        logging.step(name, "start")
        try:
            result = await coro()
        except Exception as exc:  # noqa: BLE001
            logging.step(name, "fail", detail=str(exc))
            raise
        if result:
            logging.step(name, "success")
        else:
            logging.step(name, "fail")
        return result

    async def _open_modal(self) -> bool:
        async def action() -> bool:
            try:
                await human.ensure_visible(self.trigger_button)
                await human.human_click(self.trigger_button)
            except TimeoutError:
                logging.warning("Стандартный клик не сработал, пробуем dispatchEvent")
                await close_overlays(self.page)
                await self.trigger_button.dispatch_event("click")

            try:
                await self.modal.wait_for(state="visible", timeout=8_000)
                return True
            except TimeoutError:
                if not await self.modal.count():
                    logging.error("Модалка ставки не появилась.")
                    return False
                logging.warning("Модалка отрисовалась частично, продолжаем осторожно.")
                return True

        return await self._run_step("open_modal", action)

    async def _prepare_fields(self, message: str) -> bool:
        price_input = self.modal.locator(PRICE_INPUT)
        comment_input = self.modal.locator(COMMENT_INPUT)

        async def fill_bid_amount() -> bool:
            try:
                await human.ensure_visible(price_input)
            except TimeoutError:
                raise
            await human.fill_field(
                price_input,
                str(self.cfg.bid_amount),
                enable_typos=False,
                fast=self.cfg.fast_comment_fill,
            )
            value = await price_input.input_value()
            normalized = value.replace("\xa0", "").replace(" ", "")
            return normalized == str(self.cfg.bid_amount)

        async def fill_comment() -> bool:
            await human.ensure_visible(comment_input)
            await human.fill_field(
                comment_input,
                message,
                enable_typos=not self.cfg.fast_comment_fill,
                fast=self.cfg.fast_comment_fill,
            )
            current = await comment_input.input_value()
            return current.strip() == message.strip()

        if not await self._run_step("fill_bid", fill_bid_amount):
            return False
        if not await self._run_step("fill_comment", fill_comment):
            return False
        return True

    async def _click_submit(self) -> bool:
        submit = self.modal.locator(SUBMIT_BUTTON)

        async def action() -> bool:
            await human.ensure_visible(submit)
            await submit.wait_for(state="visible", timeout=5_000)
            for _ in range(12):
                if not await submit.is_disabled():
                    break
                logging.info("Кнопка ставки неактивна, ждём перерасчёт…")
                await asyncio.sleep(0.5)
            else:
                logging.error("Кнопка ставки не активировалась.")
                return False

            await close_overlays(self.page, exclude=self.modal)

            await self.tracker.ensure_min_interval(self.cfg.min_bid_interval_seconds)

            await human.human_click(submit)
            try:
                await self.modal.wait_for(state="hidden", timeout=10_000)
                self.submission_done = True
                return True
            except TimeoutError:
                if await self.page.locator(CAPTCHA_FRAME).count():
                    logging.warning("Запрос капчи — требуется ручное вмешательство.")
                else:
                    logging.error("После отправки модалка осталась открыта.")
                return False

        return await self._run_step("click_submit", action)

    async def _fallback_close(self) -> None:
        if not await self.modal.count():
            return

        try:
            visible = await self.modal.is_visible()
        except Exception:
            visible = False

        if not visible:
            return

        selectors = (
            "button:has-text('Закрыть')",
            "button:has-text('Отмена')",
            "button:has(.uikit-button_label:has-text('Закрыть'))",
            "button.dialog-window-close-button",
        )

        for selector in selectors:
            try:
                close_btn = self.modal.locator(selector).first
                if await close_btn.count():
                    await close_btn.click(timeout=800)
                    await self.modal.wait_for(state="hidden", timeout=2_000)
                    return
            except Exception:
                continue

        try:
            await self.page.keyboard.press("Escape")
            await self.modal.wait_for(state="hidden", timeout=2_000)
        except Exception:
            pass

    async def _post_success_scroll(self) -> None:
        await human.pause((160, 280))
        await human.human_scroll(
            self.page,
            container=self.page.locator(ORDER_LIST),
            total_range=self.cfg.post_bid_scroll_range,
            step_range=(200, 240),
        )



class OrderTracker:
    ID_PATTERN = re.compile(r"/order/(?:getoneorder/)?(\d+)")

    def __init__(self, cfg: config.BidConfig) -> None:
        self.cfg = cfg
        self.attempts: Dict[str, int] = {}
        self.exhausted: Set[str] = set()
        self.seen_this_round: Set[str] = set()
        self.processed_this_cycle: Set[str] = set()  # Orders processed in current cycle (success or fail)
        self.processed_recently: Set[str] = set()  # Orders processed in recent attempts (prevents loops)
        self.processed_card_indices: Set[int] = set()  # Card indices processed in current session (fallback)
        self.current_order: Optional[str] = None
        self.current_card_index: Optional[int] = None  # Index of currently processing card
        self.last_bid_ts: float = 0.0
        self.prefer_top_window: int = 6

    def begin_cycle(self) -> None:
        self.seen_this_round.clear()
        self.processed_this_cycle.clear()

        # Clean up processed_recently if it gets too large (keep last 50)
        if len(self.processed_recently) > 50:
            # Convert to list, keep last 30, convert back to set
            recent_list = list(self.processed_recently)[-30:]
            self.processed_recently = set(recent_list)

        self.current_order = None

    async def extract_order_id(self, card: Locator) -> Optional[str]:
        try:
            href = await card.locator(CARD_TITLE_ANCHOR).get_attribute("href")
        except Exception:
            href = None
        if href:
            match = self.ID_PATTERN.search(href)
            if match:
                return match.group(1)
        try:
            data_id = await card.get_attribute("data-id")
        except Exception:
            data_id = None
        if data_id and data_id.isdigit():
            return data_id
        return None

    def can_attempt(self, order_id: Optional[str]) -> bool:
        if not order_id:
            return True
        if order_id in self.exhausted:
            return False
        if order_id in self.seen_this_round:
            return False
        return self.attempts.get(order_id, 0) < self.cfg.max_order_retries

    def mark_seen(self, order_id: Optional[str]) -> None:
        if order_id:
            self.seen_this_round.add(order_id)
            self.current_order = order_id

    def mark_exhausted(self, order_id: Optional[str]) -> None:
        if order_id:
            self.exhausted.add(order_id)
            self.processed_this_cycle.add(order_id)
            self.processed_recently.add(order_id)
            # Also mark card index as processed (fallback mechanism)
            if self.current_card_index is not None:
                self.processed_card_indices.add(self.current_card_index)

    def mark_attempt_completed(self, *, success: bool, order_id: Optional[str]) -> None:
        if not order_id:
            return
        attempts = self.attempts.get(order_id, 0) + 1
        self.attempts[order_id] = attempts
        if success or attempts >= self.cfg.max_order_retries:
            self.exhausted.add(order_id)
        # Always mark as processed in current cycle to prevent re-selection
        # This happens regardless of success/failure to prevent loops
        self.processed_this_cycle.add(order_id)
        self.processed_recently.add(order_id)

        # Also mark card index as processed (fallback mechanism)
        if self.current_card_index is not None:
            self.processed_card_indices.add(self.current_card_index)
        self.current_order = None
        loop = asyncio.get_running_loop()
        self.last_bid_ts = loop.time()

    async def ensure_min_interval(self, min_interval: float) -> None:
        loop = asyncio.get_running_loop()
        now = loop.time()
        delta = now - self.last_bid_ts
        if delta < min_interval:
            wait_for = min_interval - delta
            logging.info(f"Ждём {wait_for:.2f}с до следующей ставки (лимит интервала)")
            await asyncio.sleep(wait_for)


async def close_overlays(page: Page, *, exclude: Locator | None = None) -> None:
    dismissed = page.locator(DISMISSED_PANEL)
    if await dismissed.count():
        logging.info("Закрываем панель скрытых заказов.")
        try:
            await dismissed.locator("button").click(timeout=1_000)
        except Exception:
            pass

    exclude_handle = await exclude.element_handle() if exclude else None

    for selector, description in (
        ("div[data-testid='tooltip-close']", "подсказка"),
        ("button:has-text('Понятно')", "диалог подтверждения"),
        ("[data-testid='close-button']", "всплывающее окно"),
    ):
        overlay = page.locator(selector)
        if not await overlay.count():
            continue

        handle = await overlay.first.element_handle()
        if handle and exclude_handle:
            try:
                inside_modal = await page.evaluate(
                    "(el, root) => !!root && root.contains(el)",
                    handle,
                    exclude_handle,
                )
            except Exception:
                inside_modal = False
            if inside_modal:
                continue

        logging.info(f"Закрываем {description} ({selector})")
        try:
            await overlay.first.click(timeout=1_000)
        except Exception:
            pass


async def force_close_modal(page: Page, modal: Locator, *, aggressive: bool = False, reason: str | None = None) -> None:
    try:
        if not await modal.count():
            return
    except Exception:
        return

    try:
        visible = await modal.is_visible()
    except Exception:
        visible = False

    if not visible:
        return

    if reason:
        logging.info(reason)

    if aggressive:
        try:
            await page.mouse.click(5, 5)
        except Exception:
            pass

        await asyncio.sleep(0.2)

        try:
            await modal.wait_for(state="hidden", timeout=1_000)
            return
        except Exception:
            pass

    try:
        close_btn = modal.locator("button:has-text('Закрыть')").first
        if await close_btn.count():
            await close_btn.click(timeout=1_000)
            await modal.wait_for(state="hidden", timeout=2_000)
            return
    except Exception:
        pass

    if aggressive:
        try:
            await page.evaluate(
                "(selector) => { const node = document.querySelector(selector); if (node && node.parentElement) { node.parentElement.removeChild(node); } }",
                MODAL,
            )
        except Exception:
            pass

class HybridTrigger:
    def __init__(self, page: Page, tracker: "OrderTracker") -> None:
        self.page = page
        self.tracker = tracker
        self.priority_queue: Deque[str] = deque()
        self._running = False
        self._binding_installed = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        await self._install_dom_observer()
        self._attach_websocket_listener()

    def update_tracker(self, tracker: "OrderTracker") -> None:
        self.tracker = tracker

    async def stop_if_needed(self) -> None:
        if not self._running:
            return
        self._running = False
        self.priority_queue.clear()
        self._binding_installed = False

    async def notify_success(self, order_id: Optional[str]) -> None:
        if not order_id:
            return
        self.discard_hint(order_id)

    def iter_priority_hints(self) -> list[str]:
        hints: list[str] = []
        for _ in range(len(self.priority_queue)):
            order_id = self.priority_queue.popleft()
            if not self.tracker.can_attempt(order_id):
                continue
            hints.append(order_id)
            self.priority_queue.append(order_id)
        return hints

    def discard_hint(self, order_id: Optional[str]) -> None:
        if not order_id:
            return
        try:
            self.priority_queue.remove(order_id)
        except ValueError:
            pass

    async def _install_dom_observer(self) -> None:
        if not self._binding_installed:
            await self.page.expose_binding("__botOrderHint", lambda source, href: asyncio.create_task(self._handle_dom_hint(href)))
            self._binding_installed = True

        script = """
            (() => {
                const install = () => {
                    const emit = (href) => {
                        if (window.__botOrderHint) {
                            window.__botOrderHint(href);
                        }
                    };
                    const target = document.querySelector("div.styled__OrderListStyled-sc-anwyvn-0");
                    if (!target) return;
                    if (target.__botObserverInstalled) return;
                    target.__botObserverInstalled = true;
                    const observer = new MutationObserver((mutations) => {
                        for (const mutation of mutations) {
                            for (const node of mutation.addedNodes) {
                                if (!(node instanceof HTMLElement)) continue;
                                const anchor = node.querySelector("a[href*='/order/']");
                                if (anchor) emit(anchor.href);
                            }
                        }
                    });
                    observer.observe(target, { childList: true, subtree: true });
                };
                if (document.readyState === 'loading') {
                    document.addEventListener('DOMContentLoaded', install, { once: true });
                } else {
                    install();
                }
            })();
        """
        await self.page.add_init_script(script)
        await self.page.evaluate(script)

    def _attach_websocket_listener(self) -> None:
        def on_ws(ws):
            ws.on("framereceived", lambda frame: asyncio.create_task(self._handle_ws_frame(frame)))
        self.page.on("websocket", on_ws)

    async def _handle_ws_frame(self, frame) -> None:
        payload = getattr(frame, "payload", None)
        if payload is None and isinstance(frame, dict):
            payload = frame.get("payload")
        if not payload:
            return
        order_id = self._extract_order_id_from_payload(str(payload))
        if order_id:
            self._add_hint(order_id)

    async def _handle_dom_hint(self, href: Optional[str]) -> None:
        order_id = self._extract_order_id_from_payload(href or "")
        if order_id:
            self._add_hint(order_id)

    def _add_hint(self, order_id: str) -> None:
        if not self.tracker.can_attempt(order_id):
            return
        if order_id in self.priority_queue:
            return
        logging.info(f"Получен сигнал о новом заказе {order_id}, добавляем в очередь приоритетов")
        self.priority_queue.append(order_id)

    @staticmethod
    def _extract_order_id_from_payload(payload: str) -> Optional[str]:
        match = OrderTracker.ID_PATTERN.search(payload)
        return match.group(1) if match else None
