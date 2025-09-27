"""Filter management for order types and subjects on avtor24.ru."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Dict, List, Optional

from playwright.async_api import Locator, Page, TimeoutError

from . import config, human, logger as logging


@dataclass
class WorkType:
    """Work type data from the site."""
    id: str
    name: str
    count: int


@dataclass
class SubjectGroup:
    """Subject group containing multiple subjects."""
    name: str
    subjects: List[Subject]


@dataclass
class Subject:
    """Subject data from the site."""
    id: str
    name: str
    count: int


class FilterManager:
    """Manages application of filters to the auction page via DOM interactions."""

    def __init__(self, page: Page, filter_config: config.FilterConfig):
        self.page = page
        self.filter_config = filter_config
        self.current_filter_state: Optional[Dict[str, List[str]]] = None

    async def apply_filters_if_needed(self) -> bool:
        """Apply filters if auto_apply is enabled and filters need to be set."""
        if not self.filter_config.auto_apply_filters:
            logging.info("Автоприменение фильтров отключено")
            return True

        logging.info("Проверяем состояние фильтров на сайте...")

        try:
            # Wait for page to be ready
            await self.page.wait_for_load_state("networkidle")
            await human.pause((1000, 2000))

            # Get current filter state from page
            current_filters = await self._get_current_filters()
            desired_filters = self._get_desired_filters()

            logging.info(f"Текущие фильтры: типы работ - {len(current_filters['work_types'])}, предметы - {len(current_filters['subjects'])}")
            logging.info(f"Желаемые фильтры: типы работ - {len(desired_filters['work_types'])}, предметы - {len(desired_filters['subjects'])}")

            if self._filters_match(current_filters, desired_filters):
                logging.info("✅ Фильтры уже установлены корректно")
                return True

            logging.info("🔄 Применяем фильтры через пользовательский интерфейс...")

            # Apply filters via UI interaction
            success = await self._apply_filters_via_ui(desired_filters)

            if success:
                logging.info("✅ Фильтры применены, ожидаем обновления страницы...")
                # Wait for page to update with new filters
                await human.pause((2000, 4000))
                await self.page.wait_for_load_state("networkidle")
                await human.pause((1000, 2000))

                logging.success("✅ Фильтры успешно применены через интерфейс")
                return True
            else:
                logging.error("❌ Не удалось применить фильтры")
                return False

        except Exception as exc:
            logging.warning(f"❌ Ошибка при работе с фильтрами: {exc}")
            return False

    def _get_desired_filters(self) -> Dict[str, List[str]]:
        """Get desired filter state based on configuration."""
        return {
            "work_types": self.filter_config.enabled_work_types,
            "subjects": self.filter_config.enabled_subjects
        }

    def _filters_match(self, current: Dict[str, List[str]], desired: Dict[str, List[str]]) -> bool:
        """Check if current filters match desired filters."""
        current_work_types = set(current.get("work_types", []))
        desired_work_types = set(desired.get("work_types", []))

        current_subjects = set(current.get("subjects", []))
        desired_subjects = set(desired.get("subjects", []))

        return current_work_types == desired_work_types and current_subjects == desired_subjects

    async def _get_current_filters(self) -> Dict[str, List[str]]:
        """Get currently applied filters from page state."""
        try:
            # Try to get filters from URL parameters first
            url = self.page.url
            if "order/search" in url:
                filters_from_url = self._parse_filters_from_url(url)
                if filters_from_url:
                    return filters_from_url

            # Try to get filters from page state/localStorage
            filters_from_storage = await self._get_filters_from_storage()
            if filters_from_storage:
                return filters_from_storage

            # As fallback, assume no filters are applied
            logging.info("Не удалось определить текущие фильтры, предполагаем что их нет")
            return {"work_types": [], "subjects": []}

        except Exception as exc:
            logging.warning(f"Ошибка при получении текущих фильтров: {exc}")
            return {"work_types": [], "subjects": []}

    def _parse_filters_from_url(self, url: str) -> Optional[Dict[str, List[str]]]:
        """Parse filter parameters from URL."""
        try:
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(url)
            params = parse_qs(parsed.query)

            work_types = []
            subjects = []

            # Look for filter parameters in URL
            if 'types' in params:
                work_types = params['types']
            if 'categories' in params:
                subjects = params['categories']

            if work_types or subjects:
                logging.info(f"Найдены фильтры в URL: типы={work_types}, предметы={subjects}")
                return {"work_types": work_types, "subjects": subjects}

        except Exception as exc:
            logging.warning(f"Ошибка при парсинге URL: {exc}")

        return None

    async def _get_filters_from_storage(self) -> Optional[Dict[str, List[str]]]:
        """Try to get filters from browser localStorage or sessionStorage."""
        try:
            # Check localStorage for filter settings
            storage_data = await self.page.evaluate("""
                () => {
                    try {
                        const localData = localStorage.getItem('auction_filters');
                        const sessionData = sessionStorage.getItem('auction_filters');
                        return localData || sessionData;
                    } catch (e) {
                        return null;
                    }
                }
            """)

            if storage_data:
                # For now, skip localStorage parsing as it's complex without json import
                # In future, we could add json back if needed
                logging.info("Найдены данные фильтров в localStorage, но парсинг отключен")
                return None

        except Exception as exc:
            logging.warning(f"Ошибка при чтении localStorage: {exc}")

        return None

    async def _apply_filters_via_ui(self, desired_filters: Dict[str, List[str]]) -> bool:
        """Apply filters by interacting with the sidebar dropdowns."""
        try:
            logging.info("🔄 Применяем фильтры через интерфейс сайдбара...")

            work_types = desired_filters.get("work_types", [])
            subjects = desired_filters.get("subjects", [])

            # Apply work type filters
            if work_types:
                success = await self._apply_work_type_filters(work_types)
                if not success:
                    logging.warning("Не удалось применить фильтры типов работ")

            # Apply subject filters
            if subjects:
                success = await self._apply_subject_filters(subjects)
                if not success:
                    logging.warning("Не удалось применить фильтры предметов")

            logging.success("✅ Фильтры применены через интерфейс")
            return True

        except Exception as exc:
            logging.error(f"❌ Ошибка при применении фильтров через интерфейс: {exc}")
            return False

    async def _apply_work_type_filters(self, work_type_ids: List[str]) -> bool:
        """Apply work type filters by clicking on dropdown and checkboxes."""
        try:
            logging.info(f"Настраиваем фильтры типов работ: {len(work_type_ids)} типов")

            # Find and click on the "Типы работ" header to expand dropdown
            work_type_header = self.page.locator("p:has-text('Типы работ')").first
            if await work_type_header.count() > 0:
                await human.ensure_visible(work_type_header)
                await human.human_click(work_type_header)
                await human.pause((500, 1000))  # Wait for dropdown to expand
                logging.info("Развернули список типов работ")
            else:
                logging.warning("Не найден заголовок 'Типы работ'")
                return False

            # Check if we need all work types - if so, click "Выбрать все"
            from . import config_ui
            all_work_type_ids = set(config_ui.WORK_TYPES.keys())
            desired_work_type_ids = set(work_type_ids)

            if desired_work_type_ids == all_work_type_ids:
                # Select all work types
                select_all_btn = self.page.locator("button:has-text('Выбрать все')").first
                if await select_all_btn.count() > 0:
                    await human.ensure_visible(select_all_btn)
                    await human.human_click(select_all_btn)
                    await human.pause((500, 1000))
                    logging.info("✓ Выбрали все типы работ")
                    checked_count = len(work_type_ids)
                else:
                    logging.warning("Не найдена кнопка 'Выбрать все'")
            else:
                # Select specific work types
                checked_count = 0
                for work_type_id in work_type_ids:
                    try:
                        checkbox = await self._find_work_type_checkbox(work_type_id)
                        if checkbox:
                            is_checked = await checkbox.is_checked()
                            if not is_checked:
                                await human.ensure_visible(checkbox)
                                await human.human_click(checkbox)
                                await human.pause((200, 500))
                                logging.info(f"✓ Отметили тип работы {work_type_id}")
                                checked_count += 1
                        else:
                            logging.warning(f"Не найден чекбокс для типа работы {work_type_id}")
                    except Exception as exc:
                        logging.warning(f"Ошибка при установке типа работы {work_type_id}: {exc}")

            logging.info(f"Установлено фильтров типов работ: {checked_count}")
            return checked_count > 0

        except Exception as exc:
            logging.error(f"Ошибка при настройке фильтров типов работ: {exc}")
            return False

    async def _apply_subject_filters(self, subject_ids: List[str]) -> bool:
        """Apply subject filters by clicking on dropdown and checkboxes."""
        try:
            logging.info(f"Настраиваем фильтры предметов: {len(subject_ids)} предметов")

            # Find and click on the "Предметы" header to expand dropdown
            subject_header = self.page.locator("p:has-text('Предметы')").first
            if await subject_header.count() > 0:
                await human.ensure_visible(subject_header)
                await human.human_click(subject_header)
                await human.pause((500, 1000))  # Wait for dropdown to expand
                logging.info("Развернули список предметов")
            else:
                logging.warning("Не найден заголовок 'Предметы'")
                return False

            # Check subject checkboxes
            checked_count = 0
            for subject_id in subject_ids:
                try:
                    checkbox = await self._find_subject_checkbox(subject_id)
                    if checkbox:
                        is_checked = await checkbox.is_checked()
                        if not is_checked:
                            await human.ensure_visible(checkbox)
                            await human.human_click(checkbox)
                            await human.pause((200, 500))
                            logging.info(f"✓ Отметили предмет {subject_id}")
                            checked_count += 1
                    else:
                        logging.warning(f"Не найден чекбокс для предмета {subject_id}")
                except Exception as exc:
                    logging.warning(f"Ошибка при установке предмета {subject_id}: {exc}")

            logging.info(f"Установлено фильтров предметов: {checked_count}")
            return checked_count > 0

        except Exception as exc:
            logging.error(f"Ошибка при настройке фильтров предметов: {exc}")
            return False

    async def _find_work_type_checkbox(self, work_type_id: str) -> Optional[Locator]:
        """Find checkbox for specific work type."""
        try:
            # Import work type names mapping
            from . import config_ui

            work_type_name = config_ui.WORK_TYPES.get(work_type_id)
            if not work_type_name:
                logging.warning(f"Неизвестный ID типа работы: {work_type_id}")
                return None

            # Find checkbox by text in parent label
            # Look for label containing the work type name
            checkbox = self.page.locator(f"label:has-text('{work_type_name}') input[type='checkbox']").first
            if await checkbox.count() > 0:
                return checkbox

            # Alternative: find by class and position in the expanded dropdown
            # This is more complex but could work if text search fails

            return None

        except Exception as exc:
            logging.warning(f"Ошибка при поиске чекбокса типа работы {work_type_id}: {exc}")
            return None

    async def _find_subject_checkbox(self, subject_id: str) -> Optional[Locator]:
        """Find checkbox for specific subject."""
        try:
            # Similar to work type checkboxes
            checkbox = self.page.locator(f"input[type='checkbox'][value='{subject_id}']")
            if await checkbox.count() > 0:
                return checkbox.first

            return None

        except Exception:
            return None


