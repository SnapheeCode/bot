"""Project configuration for bidding behavior."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple, List, Optional


@dataclass(frozen=True)
class FilterConfig:
    """Configuration for order filtering by types and subjects."""
    enabled_work_types: List[str]  # IDs of work types to filter by
    enabled_subjects: List[str]    # IDs of subjects to filter by
    auto_apply_filters: bool = True  # Whether to automatically apply filters on startup


@dataclass(frozen=True)
class BidConfig:
    bid_amount: int
    greeting_template: str
    followup_template: str
    filter_config: FilterConfig
    followup_delay_minutes: float = 0.5  # Delay before sending follow-up message (30 seconds)
    max_scrolls: int = 15
    max_attempts: int = 25
    scroll_step_range: Tuple[int, int] = (640, 720)
    scroll_recovery_range: Tuple[int, int] = (220, 320)
    post_bid_scroll_range: Tuple[int, int] = (260, 320)
    max_order_retries: int = 1
    min_bid_interval_seconds: float = 3.0
    fast_comment_fill: bool = True
    smart_queue_pages_to_scan: int = 5  # Количество страниц для глубокого сканирования

    def build_message(self, title: str | None = None, work_type: str | None = None, subject: str | None = None) -> str:
        safe_title = title or "проект"
        safe_work_type = work_type or "работу"
        safe_subject = subject or "вашей теме"
        return self.greeting_template.format(
            title=safe_title,
            work_type=safe_work_type,
            subject=safe_subject,
        )


DEFAULT_FILTER_CONFIG = FilterConfig(
    enabled_work_types=[
        "2",   # Курсовая работа
        "9",   # Контрольная работа
        "11",  # Решение задач
        "3",   # Реферат
        "6",   # Статья
        "1",   # Дипломная работа
    ],
    enabled_subjects=[],  # По умолчанию все предметы
    auto_apply_filters=True,
)

DEFAULT_BID_CONFIG = BidConfig(
    bid_amount=1490,
    greeting_template=(
        "Здравствуйте! Готов выполнить {work_type} \"{title}\". "
        "Опыт по {subject}, работаю тщательно и соблюдаю сроки."
    ),
    followup_template=(
        "Напоминаю о {work_type} \"{title}\". "
        "Готов приступить к работе в любое время. Опыт по {subject}, гарантирую качество и сроки."
    ),
    filter_config=DEFAULT_FILTER_CONFIG,
    followup_delay_minutes=0.5,
    max_scrolls=20,
    max_attempts=30,
    scroll_step_range=(650, 720),
    scroll_recovery_range=(240, 320),
    post_bid_scroll_range=(280, 360),
    max_order_retries=1,
    min_bid_interval_seconds=3.0,
    fast_comment_fill=True,
    smart_queue_pages_to_scan=5,
)


