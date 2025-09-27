"""Main entrypoint for avtor24 automation prototype."""

from __future__ import annotations

import asyncio
import signal
import sys
from typing import Optional

from playwright.async_api import async_playwright

from . import auction, browser, config, graphql_client, logger as logging, message_scheduler, network_monitor, settings


class Avtor24Bot:
    """Main bot class managing parallel browser and API operations."""

    def __init__(self):
        self.playwright: Optional[async_playwright] = None
        self.page = None
        self.context = None
        self.gql_client: Optional[graphql_client.GraphQLClient] = None
        self.msg_scheduler: Optional[message_scheduler.SimpleMessageScheduler] = None
        self.network_monitor: Optional[network_monitor.NetworkMonitor] = None
        self.cfg: Optional[config.BidConfig] = None
        self.running = False
        self.bot_task: Optional[asyncio.Task] = None

    async def initialize(self):
        """Initialize browser and API components."""
        try:
            # Initialize browser
            self.playwright = await async_playwright().start()
            self.context = await browser.start_browser(self.playwright, headless=False)
            self.page = self.context.pages[0]

            # Load configuration
            self.cfg = settings.settings_manager.load_settings()

            # Initialize GraphQL client
            self.gql_client = graphql_client.GraphQLClient(self.page)

            # Initialize message scheduler
            self.msg_scheduler = message_scheduler.SimpleMessageScheduler(self.gql_client, self.cfg)
            await self.msg_scheduler.start()

            # Initialize network monitor (disabled for cleaner logs)
            self.network_monitor = network_monitor.NetworkMonitor(self.page, enabled=False)
            # logging.info("📊 Сетевой монитор инициализирован")

            logging.info("✅ Инициализация завершена успешно")

        except Exception as exc:
            logging.error(f"❌ Ошибка инициализации: {exc}")
            await self.cleanup()
            raise

    async def run_browser_bot(self):
        """Run the browser-based bidding bot with smart order queue."""
        dual_manager = None

        try:
            await browser.wait_for_manual_login(self.page, self.cfg)
            logging.info("🔐 Авторизация подтверждена, начинаем обработку ленты...")

            # Создаем вторую вкладку для мониторинга
            monitor_page = await self.page.context.new_page()
            logging.info("📄 Создана вторая вкладка для мониторинга новых заказов")

            # Инициализируем DualBrowserManager
            dual_manager = auction.DualBrowserManager(self.page, monitor_page, self.cfg)

            # Запускаем мониторинг новых заказов
            await dual_manager.start_monitoring()

            # Основной цикл обработки заказов из умной очереди
            attempts = 0
            while self.running and attempts < self.cfg.max_attempts:
                attempts += 1
                logging.info(f"🎯 Попытка #{attempts}")

                try:
                    # Получить следующий заказ из очереди
                    order_data = dual_manager.get_next_order()

                    if order_data:
                        logging.info(f"🎯 Обрабатываем заказ {order_data['id']}: {order_data['title'][:50]}...")
                        success = await self._process_order_from_queue(order_data)

                        if success:
                            logging.success(f"✅ Сработали по заказу {order_data['id']}")
                            # Соблюдаем интервал между ставками
                            await asyncio.sleep(self.cfg.min_bid_interval_seconds)
                        else:
                            logging.info(f"⏭️ Не удалось обработать заказ {order_data['id']}")
                    else:
                        # Очередь пуста, ждем новых заказов
                        logging.info("📋 Очередь пуста, ждем новых заказов...")
                        await asyncio.sleep(10)

                except Exception as exc:
                    logging.error(f"❌ Ошибка в цикле ставок: {exc}")
                    await asyncio.sleep(5)  # Brief pause on error

            if attempts >= self.cfg.max_attempts:
                logging.warning("🛑 Достигнут лимит попыток")

        except Exception as exc:
            logging.error(f"❌ Критическая ошибка в браузерном боте: {exc}")
        finally:
            # Останавливаем мониторинг
            if dual_manager:
                await dual_manager.stop_monitoring()
            logging.info("🛑 Браузерный бот завершен")

    async def _process_order_from_queue(self, order_data: dict) -> bool:
        """Обработать заказ из умной очереди."""
        try:
            # Перейти по URL заказа
            order_url = order_data.get('url')
            if not order_url:
                logging.error(f"❌ Нет URL для заказа {order_data.get('id')}")
                return False

            logging.debug(f"🔗 Переходим к заказу: {order_url}")
            await self.page.goto(order_url, wait_until="networkidle")
            await asyncio.sleep(1)  # Дать странице загрузиться

            # Выполнить ставку через обычную логику
            success = await auction.place_bid(self.page, self.cfg, self.msg_scheduler, self.gql_client)

            if success:
                # Пометить заказ как обработанный в очереди
                order_id = order_data.get('id')
                if hasattr(self, 'dual_manager') and self.dual_manager:
                    self.dual_manager.order_queue.mark_processed(order_id)

            return success

        except Exception as exc:
            logging.error(f"❌ Ошибка обработки заказа {order_data.get('id')}: {exc}")
            return False

    async def run_parallel_operations(self):
        """Run browser bot and API scheduler in parallel."""
        self.running = True

        try:
            # Create browser bot task
            self.bot_task = asyncio.create_task(self.run_browser_bot())

            logging.info("🚀 Запущены параллельные операции: браузерный бот + API планировщик")
            logging.info("📊 Бот сканирует заказы и делает ставки, API отправляет догоняющие сообщения")
            logging.info("🔄 Оба компонента работают параллельно и независимо")

            # Create monitoring task
            monitor_task = asyncio.create_task(self._monitor_components())

            # Wait for browser bot to complete (API scheduler and monitor run in background)
            await self.bot_task

            # Cancel monitor task
            if not monitor_task.done():
                monitor_task.cancel()
                try:
                    await monitor_task
                except asyncio.CancelledError:
                    pass

        except Exception as exc:
            logging.error(f"❌ Критическая ошибка в параллельных операциях: {exc}")
            # Try to restart components if they failed
            await self._attempt_recovery()
        finally:
            self.running = False

    async def _monitor_components(self):
        """Monitor health of both components during operation."""
        while self.running:
            try:
                await asyncio.sleep(300)  # Check every 5 minutes

                # Check message scheduler status
                if self.msg_scheduler:
                    scheduler_status = self.msg_scheduler.get_status()
                    if not scheduler_status.get("running", False):
                        logging.warning("⚠️ Планировщик сообщений остановлен, перезапускаем...")
                        await self.msg_scheduler.start()
                    else:
                        # Log queue stats periodically
                        queue_stats = scheduler_status.get("queue_stats", {})
                        pending = queue_stats.get("pending", 0) + queue_stats.get("failed", 0)
                        if pending > 0:
                            logging.info(f"📊 Статус очереди: {pending} сообщений ожидают отправки")

                # Check browser health
                if self.page:
                    try:
                        await self.page.title()  # Simple health check
                    except Exception as exc:
                        logging.warning(f"⚠️ Проблема с браузером: {exc}")

                # Log network monitor stats
                if self.network_monitor:
                    net_stats = self.network_monitor.get_stats()
                    if net_stats.get("graphql_requests", 0) > 0:
                        logging.info(f"🌐 API: {net_stats['graphql_requests']} GraphQL, {net_stats['api_errors']} ошибок")

                    # Log recent GraphQL operations
                    recent_gql = self.network_monitor.get_recent_graphql(3)
                    for gql in recent_gql:
                        op = gql.get("operation", "unknown")
                        ts = gql.get("timestamp", "")[-8:]  # Last 8 chars of timestamp
                        logging.debug(f"🔍 {ts} {op}")

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logging.error(f"❌ Ошибка в мониторинге компонентов: {exc}")
                await asyncio.sleep(60)  # Wait before retrying

    async def _attempt_recovery(self):
        """Attempt to recover from critical errors."""
        logging.info("🔧 Попытка восстановления после критической ошибки...")

        try:
            # Check if message scheduler is still running
            if self.msg_scheduler and self.msg_scheduler.running:
                scheduler_status = self.msg_scheduler.get_status()
                if not scheduler_status.get("scheduler_task_active", False):
                    logging.warning("📅 Планировщик сообщений не активен, перезапускаем...")
                    await self.msg_scheduler.stop()
                    await self.msg_scheduler.start()
                    logging.info("✅ Планировщик сообщений перезапущен")
        except Exception as exc:
            logging.error(f"❌ Не удалось восстановить планировщик: {exc}")

        try:
            # Check browser connection
            if self.page:
                # Simple health check - try to get page title
                await self.page.title()
                logging.info("✅ Браузер работает нормально")
            else:
                logging.warning("⚠️ Браузер недоступен")
        except Exception as exc:
            logging.error(f"❌ Проблема с браузером: {exc}")

    async def cleanup(self):
        """Clean up resources."""
        logging.info("🧹 Начинаем очистку ресурсов...")

        # Stop message scheduler
        if self.msg_scheduler:
            try:
                await self.msg_scheduler.stop()
                logging.info("✅ API планировщик остановлен")
            except Exception as exc:
                logging.error(f"❌ Ошибка остановки планировщика: {exc}")

        # Close browser
        if self.context:
            try:
                await self.context.close()
                logging.info("✅ Браузер закрыт")
            except Exception as exc:
                logging.error(f"❌ Ошибка закрытия браузера: {exc}")

        # Stop playwright
        if self.playwright:
            try:
                await self.playwright.stop()
                logging.info("✅ Playwright остановлен")
            except Exception as exc:
                logging.error(f"❌ Ошибка остановки Playwright: {exc}")

        logging.info("🧹 Очистка завершена")

    async def run(self):
        """Main run method with proper cleanup."""
        try:
            await self.initialize()
            await self.run_parallel_operations()
        except KeyboardInterrupt:
            logging.info("🛑 Получен сигнал прерывания")
        except Exception as exc:
            logging.error(f"❌ Критическая ошибка: {exc}")
        finally:
            await self.cleanup()


async def main() -> None:
    """Main entry point."""
    # Handle graceful shutdown
    def signal_handler(signum, frame):
        logging.info(f"📴 Получен сигнал {signum}, завершаем работу...")
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    bot = Avtor24Bot()
    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())


