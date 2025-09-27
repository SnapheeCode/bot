"""Persistent settings management for bot configuration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Any, Optional

from . import config


SETTINGS_FILE = Path("bot_settings.json")


class SettingsManager:
    """Manages persistent storage of bot settings."""

    def __init__(self):
        self.settings_file = SETTINGS_FILE

    def load_settings(self) -> config.BidConfig:
        """Load settings from file or return defaults."""
        try:
            if self.settings_file.exists():
                with open(self.settings_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                return self._dict_to_config(data)
        except Exception:
            pass

        return config.DEFAULT_BID_CONFIG

    def save_settings(self, cfg: config.BidConfig) -> None:
        """Save current configuration to file."""
        try:
            data = self._config_to_dict(cfg)
            with open(self.settings_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            print(f"Не удалось сохранить настройки: {exc}")

    def _config_to_dict(self, cfg: config.BidConfig) -> Dict[str, Any]:
        """Convert BidConfig to dictionary."""
        return {
            "bid_amount": cfg.bid_amount,
            "greeting_template": cfg.greeting_template,
            "followup_template": cfg.followup_template,
            "followup_delay_minutes": cfg.followup_delay_minutes,
            "filter_config": {
                "enabled_work_types": cfg.filter_config.enabled_work_types,
                "enabled_subjects": cfg.filter_config.enabled_subjects,
                "auto_apply_filters": cfg.filter_config.auto_apply_filters,
            },
            "max_scrolls": cfg.max_scrolls,
            "max_attempts": cfg.max_attempts,
            "scroll_step_range": list(cfg.scroll_step_range),
            "scroll_recovery_range": list(cfg.scroll_recovery_range),
            "post_bid_scroll_range": list(cfg.post_bid_scroll_range),
            "max_order_retries": cfg.max_order_retries,
            "min_bid_interval_seconds": cfg.min_bid_interval_seconds,
            "fast_comment_fill": cfg.fast_comment_fill,
            "smart_queue_pages_to_scan": cfg.smart_queue_pages_to_scan,
        }

    def _dict_to_config(self, data: Dict[str, Any]) -> config.BidConfig:
        """Convert dictionary to BidConfig."""
        filter_data = data.get("filter_config", {})
        filter_config = config.FilterConfig(
            enabled_work_types=filter_data.get("enabled_work_types", config.DEFAULT_FILTER_CONFIG.enabled_work_types),
            enabled_subjects=filter_data.get("enabled_subjects", config.DEFAULT_FILTER_CONFIG.enabled_subjects),
            auto_apply_filters=filter_data.get("auto_apply_filters", config.DEFAULT_FILTER_CONFIG.auto_apply_filters),
        )

        return config.BidConfig(
            bid_amount=data.get("bid_amount", config.DEFAULT_BID_CONFIG.bid_amount),
            greeting_template=data.get("greeting_template", config.DEFAULT_BID_CONFIG.greeting_template),
            followup_template=data.get("followup_template", config.DEFAULT_BID_CONFIG.followup_template),
            followup_delay_minutes=data.get("followup_delay_minutes", config.DEFAULT_BID_CONFIG.followup_delay_minutes),
            filter_config=filter_config,
            max_scrolls=data.get("max_scrolls", config.DEFAULT_BID_CONFIG.max_scrolls),
            max_attempts=data.get("max_attempts", config.DEFAULT_BID_CONFIG.max_attempts),
            scroll_step_range=tuple(data.get("scroll_step_range", config.DEFAULT_BID_CONFIG.scroll_step_range)),
            scroll_recovery_range=tuple(data.get("scroll_recovery_range", config.DEFAULT_BID_CONFIG.scroll_recovery_range)),
            post_bid_scroll_range=tuple(data.get("post_bid_scroll_range", config.DEFAULT_BID_CONFIG.post_bid_scroll_range)),
            max_order_retries=data.get("max_order_retries", config.DEFAULT_BID_CONFIG.max_order_retries),
            min_bid_interval_seconds=data.get("min_bid_interval_seconds", config.DEFAULT_BID_CONFIG.min_bid_interval_seconds),
            fast_comment_fill=data.get("fast_comment_fill", config.DEFAULT_BID_CONFIG.fast_comment_fill),
            smart_queue_pages_to_scan=data.get("smart_queue_pages_to_scan", config.DEFAULT_BID_CONFIG.smart_queue_pages_to_scan),
        )


# Global settings manager instance
settings_manager = SettingsManager()
