"""Simple message scheduler for sending follow-up messages after bids."""

import asyncio
from typing import Optional, Dict, Any
from datetime import datetime, timedelta

from . import logger as logging
from .graphql_client import GraphQLClient
from .config import BidConfig
from .sqlite_queue import SQLiteMessageQueue


class SimpleMessageScheduler:
    """Simple scheduler that checks queue every 5 minutes and sends due messages."""

    def __init__(self, graphql_client: GraphQLClient, cfg: BidConfig, db_path: str = "message_queue.db"):
        self.graphql_client = graphql_client
        self.cfg = cfg
        self.queue = SQLiteMessageQueue(db_path)
        self.scheduler_task: Optional[asyncio.Task] = None
        self.running = False

    async def start(self):
        """Start the simple message scheduler."""
        if self.running:
            return

        self.running = True
        self.scheduler_task = asyncio.create_task(self._scheduler_loop())
        logging.info("🚀 Запущен простой планировщик сообщений")
        logging.info("⏰ Проверка каждые 30 секунд")

        # Clean up old messages on startup
        cleaned = self.queue.cleanup_old_messages(days=7)
        if cleaned > 0:
            logging.info(f"🧹 Очищено {cleaned} старых сообщений")

    async def stop(self):
        """Stop the message scheduler gracefully."""
        if not self.running:
            return

        logging.info("🛑 Останавливаем планировщик догоняющих сообщений...")
        self.running = False

        if self.scheduler_task and not self.scheduler_task.done():
            self.scheduler_task.cancel()
            try:
                await asyncio.wait_for(self.scheduler_task, timeout=10.0)
                logging.info("✅ Планировщик остановлен gracefully")
            except asyncio.TimeoutError:
                logging.warning("⏰ Планировщик не остановился за 10 секунд, принудительно завершаем")
            except asyncio.CancelledError:
                logging.info("✅ Планировщик успешно отменен")
            except Exception as exc:
                logging.error(f"❌ Ошибка при остановке планировщика: {exc}")

        logging.info("🛑 Планировщик догоняющих сообщений остановлен")

    def schedule_followup_message(self, order_id: str, customer_id: str, order_title: str,
                                subject_name: str, work_type_name: str, delay_minutes: int = 10):
        """Schedule a follow-up message to be sent after specified delay."""
        success = self.queue.schedule_message(
            order_id=order_id,
            customer_id=customer_id,
            order_title=order_title,
            subject_name=subject_name,
            work_type_name=work_type_name,
            delay_minutes=delay_minutes,
            message_template=self.cfg.followup_template
        )

        if success:
            logging.info(f"📅 Запланировано догоняющее сообщение для заказа {order_id} через {delay_minutes} мин, заказчик: {customer_id}")
        else:
            logging.error(f"❌ Не удалось запланировать сообщение для заказа {order_id}")

        return success

    async def _scheduler_loop(self):
        """Simple scheduler loop - check every 30 seconds and send due messages."""
        while self.running:
            try:
                # Wait 30 seconds between checks
                await asyncio.sleep(30)  # 30 seconds

                # Get all due messages
                due_messages = self.queue.get_due_messages(limit=50)
                logging.info(f"🔍 Проверка очереди: найдено {len(due_messages)} просроченных сообщений")

                if due_messages:
                    logging.info(f"📨 Отправляем {len(due_messages)} догоняющих сообщений")

                    sent_count = 0
                    for message in due_messages:
                        logging.info(f"📤 Отправка в заказ {message.order_id} (задержка {self.cfg.followup_delay_minutes} мин)")
                        try:
                            await self._send_message(message)
                            sent_count += 1
                            # Small delay between messages to avoid rate limiting
                            await asyncio.sleep(1)
                        except Exception as exc:
                            logging.error(f"❌ Ошибка отправки в заказ {message.order_id}: {exc}")
                            # Mark as failed - will be retried on next check
                            self.queue.mark_failed(message.id, str(exc))

                    logging.info(f"✅ Отправлено {sent_count} сообщений")
                else:
                    logging.debug("📭 Просроченных сообщений нет")

            except asyncio.CancelledError:
                logging.info("🛑 Планировщик остановлен")
                break
            except Exception as exc:
                logging.error(f"❌ Ошибка в планировщике: {exc}")
                # Wait before retrying
                await asyncio.sleep(60)

    async def _send_message(self, message):
        """Send a follow-up message for the given order."""
        try:
            # Get customer_id if we don't have it
            customer_id = message.customer_id
            if customer_id == "unknown":
                customer_id = await self.graphql_client.get_customer_id(message.order_id)
                if not customer_id:
                    logging.warning(f"⚠️ Не найден customer_id для заказа {message.order_id}")
                    self.queue.mark_failed(message.id, "Customer ID not found")
                    return

            # Format message
            text = message.message_template.format(
                work_type=message.work_type_name,
                title=message.order_title,
                subject=message.subject_name
            )

            # Send via API
            result = await self.graphql_client.send_message(message.order_id, text)

            if result and result.get("data", {}).get("addComment"):
                logging.success(f"✅ Догоняющее сообщение в заказ {message.order_id}")
                self.queue.mark_sent(message.id)
            else:
                logging.warning(f"⚠️ Сообщение в заказ {message.order_id} не отправлено")
                self.queue.mark_failed(message.id, "API returned no data")

        except Exception as exc:
            logging.error(f"❌ Ошибка отправки в заказ {message.order_id}: {exc}")
            self.queue.mark_failed(message.id, str(exc))
            raise

    def get_status(self) -> Dict[str, Any] | None:
        """Get current scheduler status."""
        stats = self.queue.get_stats()
        return {
            "running": self.running,
            "queue_stats": stats,
            "scheduler_task_active": self.scheduler_task is not None and not self.scheduler_task.done() if self.scheduler_task else False
        }

