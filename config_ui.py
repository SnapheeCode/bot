"""Console-based configuration interface for bot settings."""

from __future__ import annotations

import os
from typing import List

from . import config, settings


# Work type mappings (ID -> Name)
WORK_TYPES = {
    "1": "Дипломная работа",
    "2": "Курсовая работа",
    "3": "Реферат",
    "4": "Отчёт по практике",
    "5": "Лабораторная работа",
    "6": "Статья",
    "7": "Доклад",
    "8": "Презентация",
    "9": "Контрольная работа",
    "10": "Тест",
    "11": "Решение задач",
    "12": "Чертеж",
    "13": "Макет",
    "14": "Модель",
    "15": "Программа",
    "16": "Приложение",
    "17": "Сайт",
    "18": "База данных",
    "19": "Презентации",
    "20": "Видео",
    "21": "Другое",
    "22": "Эссе",
    "23": "Рецензия",
    "24": "Аннотация",
    "89": "Магистерская диссертация",
    "123": "Бакалаврская работа",
    "124": "Кандидатская диссертация",
    "125": "Докторская диссертация",
    "126": "Выпускная квалификационная работа (ВКР)",
    "127": "Научная статья",
    "159": "Другое (предмет)",
    "160": "Математика",
    "161": "Физика",
    "162": "Химия",
    "163": "Биология",
    "164": "История",
    "165": "Литература",
}


def clear_screen():
    """Clear console screen."""
    os.system('clear' if os.name == 'posix' else 'cls')


def print_header():
    """Print application header."""
    print("=" * 50)
    print("    Avtor24 Bot - Настройки")
    print("=" * 50)
    print()


def show_main_menu():
    """Show main configuration menu."""
    cfg = settings.settings_manager.load_settings()

    while True:
        clear_screen()
        print_header()

        print("Текущие настройки:")
        print(f"Сумма ставки: {cfg.bid_amount} ₽")
        print(f"Интервал между ставками: {cfg.min_bid_interval_seconds} сек")
        print(f"Автоприменение фильтров: {'ВКЛ' if cfg.filter_config.auto_apply_filters else 'ВЫКЛ'}")
        print(f"Типы работ: {len(cfg.filter_config.enabled_work_types)} выбрано")
        print(f"Предметы: {len(cfg.filter_config.enabled_subjects)} выбрано")
        print()

        print("Меню:")
        print("1. Изменить сумму ставки")
        print("2. Изменить интервал между ставками")
        print("3. Настроить шаблон догоняющего сообщения")
        print("4. Настроить фильтры по типам работ")
        print("5. Настроить фильтры по предметам")
        print("6. Переключить автоприменение фильтров")
        print("7. Сбросить настройки к умолчанию")
        print("8. Сохранить и выйти")
        print("0. Выйти без сохранения")
        print()

        choice = input("Выберите опцию: ").strip()

        if choice == "1":
            configure_bid_amount(cfg)
        elif choice == "2":
            configure_bid_interval(cfg)
        elif choice == "3":
            configure_followup_template(cfg)
        elif choice == "4":
            configure_work_types(cfg)
        elif choice == "5":
            configure_subjects(cfg)
        elif choice == "6":
            toggle_auto_filters(cfg)
        elif choice == "7":
            reset_to_defaults(cfg)
        elif choice == "8":
            settings.settings_manager.save_settings(cfg)
            print("Настройки сохранены!")
            break
        elif choice == "0":
            break
        else:
            input("Неверный выбор. Нажмите Enter для продолжения...")


def configure_bid_amount(cfg: config.BidConfig):
    """Configure bid amount."""
    clear_screen()
    print_header()
    print(f"Текущая сумма ставки: {cfg.bid_amount} ₽")
    print()

    try:
        new_amount = int(input("Введите новую сумму ставки (₽): ").strip())
        if new_amount > 0:
            cfg.bid_amount = new_amount
            print(f"Сумма ставки изменена на {new_amount} ₽")
        else:
            print("Сумма должна быть положительной!")
    except ValueError:
        print("Неверный формат числа!")

    input("\nНажмите Enter для продолжения...")


def configure_bid_interval(cfg: config.BidConfig):
    """Configure bid interval."""
    clear_screen()
    print_header()
    print(f"Текущий интервал: {cfg.min_bid_interval_seconds} сек")
    print()

    try:
        new_interval = float(input("Введите новый интервал между ставками (сек): ").strip())
        if new_interval >= 1.0:
            cfg.min_bid_interval_seconds = new_interval
            print(f"Интервал изменен на {new_interval} сек")
        else:
            print("Интервал должен быть не менее 1 секунды!")
    except ValueError:
        print("Неверный формат числа!")

    input("\nНажмите Enter для продолжения...")


def configure_work_types(cfg: config.BidConfig):
    """Configure work types filter."""
    while True:
        clear_screen()
        print_header()
        print("Фильтр по типам работ:")
        print()

        # Show current selection
        print("Выбранные типы работ:")
        if cfg.filter_config.enabled_work_types:
            for work_type_id in cfg.filter_config.enabled_work_types:
                name = WORK_TYPES.get(work_type_id, f"Неизвестный ({work_type_id})")
                print(f"  ✓ {name}")
        else:
            print("  Все типы работ")
        print()

        # Show available options
        print("Доступные типы работ:")
        for i, (work_type_id, name) in enumerate(WORK_TYPES.items(), 1):
            status = "✓" if work_type_id in cfg.filter_config.enabled_work_types else " "
            print(f"{i:2d}. [{status}] {name}")

        print()
        print("Опции:")
        print("1-39. Переключить тип работы")
        print("all. Выбрать все")
        print("none. Снять все")
        print("0. Назад")
        print()

        choice = input("Выберите опцию: ").strip().lower()

        if choice == "0":
            break
        elif choice == "all":
            cfg.filter_config.enabled_work_types = list(WORK_TYPES.keys())
            print("Выбраны все типы работ")
        elif choice == "none":
            cfg.filter_config.enabled_work_types = []
            print("Сняты все фильтры по типам работ")
        else:
            try:
                index = int(choice) - 1
                if 0 <= index < len(WORK_TYPES):
                    work_type_id = list(WORK_TYPES.keys())[index]
                    if work_type_id in cfg.filter_config.enabled_work_types:
                        cfg.filter_config.enabled_work_types.remove(work_type_id)
                        print(f"Снят фильтр: {WORK_TYPES[work_type_id]}")
                    else:
                        cfg.filter_config.enabled_work_types.append(work_type_id)
                        print(f"Добавлен фильтр: {WORK_TYPES[work_type_id]}")
                else:
                    print("Неверный номер!")
            except ValueError:
                print("Неверный формат!")

        input("\nНажмите Enter для продолжения...")


def configure_subjects(cfg: config.BidConfig):
    """Configure subjects filter."""
    clear_screen()
    print_header()
    print("Фильтр по предметам:")
    print()
    print("Примечание: Фильтр по предметам пока не реализован в полном объеме.")
    print("Пока что можно только отключить все фильтры (выбрать все предметы)")
    print()

    if cfg.filter_config.enabled_subjects:
        print(f"Выбрано предметов: {len(cfg.filter_config.enabled_subjects)}")
        print("Для сброса фильтров по предметам выберите 'Сбросить'")
    else:
        print("Выбраны все предметы (фильтр отключен)")

    print()
    print("Опции:")
    print("1. Сбросить фильтр (выбрать все предметы)")
    print("0. Назад")
    print()

    choice = input("Выберите опцию: ").strip()

    if choice == "1":
        cfg.filter_config.enabled_subjects = []
        print("Фильтр по предметам сброшен - выбраны все предметы")
        input("\nНажмите Enter для продолжения...")
    elif choice == "0":
        return
    else:
        input("Неверный выбор. Нажмите Enter для продолжения...")


def configure_followup_template(cfg: config.BidConfig):
    """Configure follow-up message template."""
    clear_screen()
    print_header()
    print("Настройка шаблона догоняющего сообщения")
    print("=" * 40)
    print()
    print("Текущий шаблон:")
    print(f'"{cfg.followup_template}"')
    print()
    print("Доступные переменные:")
    print("{work_type} - тип работы")
    print("{title} - название заказа")
    print("{subject} - предмет")
    print()
    print("Введите новый шаблон (или оставьте пустым для отмены):")

    new_template = input().strip()
    if new_template:
        cfg.followup_template = new_template
        print("Шаблон обновлен!")
    else:
        print("Шаблон не изменен.")

    input("\nНажмите Enter для продолжения...")


def toggle_auto_filters(cfg: config.BidConfig):
    """Toggle auto-apply filters setting."""
    cfg.filter_config.auto_apply_filters = not cfg.filter_config.auto_apply_filters
    status = "ВКЛЮЧЕНО" if cfg.filter_config.auto_apply_filters else "ОТКЛЮЧЕНО"
    print(f"Автоприменение фильтров: {status}")
    input("\nНажмите Enter для продолжения...")


def reset_to_defaults(cfg: config.BidConfig):
    """Reset all settings to defaults."""
    # This will create a new instance with defaults
    default_cfg = config.DEFAULT_BID_CONFIG

    # Copy default values to current config
    cfg.bid_amount = default_cfg.bid_amount
    cfg.greeting_template = default_cfg.greeting_template
    cfg.followup_template = default_cfg.followup_template
    cfg.filter_config = config.FilterConfig(
        enabled_work_types=default_cfg.filter_config.enabled_work_types.copy(),
        enabled_subjects=default_cfg.filter_config.enabled_subjects.copy(),
        auto_apply_filters=default_cfg.filter_config.auto_apply_filters,
    )
    cfg.max_scrolls = default_cfg.max_scrolls
    cfg.max_attempts = default_cfg.max_attempts
    cfg.scroll_step_range = default_cfg.scroll_step_range
    cfg.scroll_recovery_range = default_cfg.scroll_recovery_range
    cfg.post_bid_scroll_range = default_cfg.post_bid_scroll_range
    cfg.max_order_retries = default_cfg.max_order_retries
    cfg.min_bid_interval_seconds = default_cfg.min_bid_interval_seconds
    cfg.fast_comment_fill = default_cfg.fast_comment_fill

    print("Все настройки сброшены к значениям по умолчанию!")
    input("\nНажмите Enter для продолжения...")


def main():
    """Main function for configuration interface."""
    show_main_menu()


if __name__ == "__main__":
    main()
