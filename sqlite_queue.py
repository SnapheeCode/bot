"""SQLite-based persistent queue for scheduled messages."""

import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from pathlib import Path
import json


@dataclass
class ScheduledMessage:
    """Represents a scheduled follow-up message."""
    id: Optional[int]
    order_id: str
    customer_id: str
    order_title: str
    subject_name: str
    work_type_name: str
    scheduled_time: datetime
    message_template: str
    status: str = "pending"  # pending, sent, failed, cancelled
    created_at: datetime = None
    sent_at: Optional[datetime] = None
    retry_count: int = 0
    max_retries: int = 3
    last_error: Optional[str] = None

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now()

    @property
    def is_due(self) -> bool:
        """Check if the message is due to be sent."""
        return datetime.now() >= self.scheduled_time

    @property
    def time_remaining(self) -> timedelta:
        """Get remaining time until the message should be sent."""
        return self.scheduled_time - datetime.now()

    @property
    def can_retry(self) -> bool:
        """Check if message can be retried."""
        return self.retry_count < self.max_retries and self.status in ["pending", "failed"]


class SQLiteMessageQueue:
    """Persistent SQLite-based queue for scheduled messages."""

    def __init__(self, db_path: str = "message_queue.db"):
        self.db_path = Path(db_path)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        """Initialize database schema."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS scheduled_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT NOT NULL,
                    customer_id TEXT NOT NULL,
                    order_title TEXT NOT NULL,
                    subject_name TEXT NOT NULL,
                    work_type_name TEXT NOT NULL,
                    scheduled_time TEXT NOT NULL,
                    message_template TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    sent_at TEXT,
                    retry_count INTEGER DEFAULT 0,
                    max_retries INTEGER DEFAULT 3,
                    last_error TEXT,
                    UNIQUE(order_id)
                )
            """)

            # Create indexes for performance
            conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON scheduled_messages(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_scheduled_time ON scheduled_messages(scheduled_time)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_order_id ON scheduled_messages(order_id)")

            conn.commit()

    def _row_to_message(self, row) -> ScheduledMessage:
        """Convert database row to ScheduledMessage object."""
        return ScheduledMessage(
            id=row[0],
            order_id=row[1],
            customer_id=row[2],
            order_title=row[3],
            subject_name=row[4],
            work_type_name=row[5],
            scheduled_time=datetime.fromisoformat(row[6]),
            message_template=row[7],
            status=row[8],
            created_at=datetime.fromisoformat(row[9]),
            sent_at=datetime.fromisoformat(row[10]) if row[10] else None,
            retry_count=row[11],
            max_retries=row[12],
            last_error=row[13]
        )

    def _message_to_row(self, msg: ScheduledMessage) -> tuple:
        """Convert ScheduledMessage to database row."""
        return (
            msg.order_id,
            msg.customer_id,
            msg.order_title,
            msg.subject_name,
            msg.work_type_name,
            msg.scheduled_time.isoformat(),
            msg.message_template,
            msg.status,
            msg.created_at.isoformat(),
            msg.sent_at.isoformat() if msg.sent_at else None,
            msg.retry_count,
            msg.max_retries,
            msg.last_error
        )

    def schedule_message(self, order_id: str, customer_id: str, order_title: str,
                        subject_name: str, work_type_name: str,
                        delay_minutes: int = 10, message_template: str = "") -> bool:
        """Schedule a new message for sending."""
        scheduled_time = datetime.now() + timedelta(minutes=delay_minutes)
        message = ScheduledMessage(
            id=None,
            order_id=order_id,
            customer_id=customer_id,
            order_title=order_title,
            subject_name=subject_name,
            work_type_name=work_type_name,
            scheduled_time=scheduled_time,
            message_template=message_template
        )

        with self._lock, sqlite3.connect(self.db_path) as conn:
            try:
                # Use INSERT OR REPLACE to handle duplicates
                conn.execute("""
                    INSERT OR REPLACE INTO scheduled_messages
                    (order_id, customer_id, order_title, subject_name, work_type_name,
                     scheduled_time, message_template, status, created_at, sent_at,
                     retry_count, max_retries, last_error)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, self._message_to_row(message))
                conn.commit()
                return True
            except Exception as e:
                print(f"Error scheduling message: {e}")
                return False

    def get_due_messages(self, limit: int = 10) -> List[ScheduledMessage]:
        """Get messages that are due to be sent."""
        now = datetime.now().isoformat()
        with self._lock, sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT * FROM scheduled_messages
                WHERE status IN ('pending', 'failed')
                  AND scheduled_time <= ?
                  AND retry_count < max_retries
                ORDER BY scheduled_time ASC
                LIMIT ?
            """, (now, limit))

            return [self._row_to_message(row) for row in cursor.fetchall()]

    def get_pending_messages(self, limit: int = 50) -> List[ScheduledMessage]:
        """Get all pending messages."""
        with self._lock, sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT * FROM scheduled_messages
                WHERE status IN ('pending', 'failed')
                  AND retry_count < max_retries
                ORDER BY scheduled_time ASC
                LIMIT ?
            """, (limit,))

            return [self._row_to_message(row) for row in cursor.fetchall()]

    def mark_sent(self, message_id: int, sent_at: Optional[datetime] = None) -> bool:
        """Mark message as successfully sent."""
        if sent_at is None:
            sent_at = datetime.now()

        with self._lock, sqlite3.connect(self.db_path) as conn:
            try:
                conn.execute("""
                    UPDATE scheduled_messages
                    SET status = 'sent', sent_at = ?, last_error = NULL
                    WHERE id = ?
                """, (sent_at.isoformat(), message_id))
                conn.commit()
                return True
            except Exception as e:
                print(f"Error marking message sent: {e}")
                return False

    def mark_failed(self, message_id: int, error: str) -> bool:
        """Mark message as failed and increment retry count."""
        with self._lock, sqlite3.connect(self.db_path) as conn:
            try:
                conn.execute("""
                    UPDATE scheduled_messages
                    SET status = 'failed', retry_count = retry_count + 1, last_error = ?
                    WHERE id = ?
                """, (error, message_id))
                conn.commit()
                return True
            except Exception as e:
                print(f"Error marking message failed: {e}")
                return False

    def cancel_message(self, order_id: str) -> bool:
        """Cancel a scheduled message."""
        with self._lock, sqlite3.connect(self.db_path) as conn:
            try:
                conn.execute("""
                    UPDATE scheduled_messages
                    SET status = 'cancelled'
                    WHERE order_id = ? AND status IN ('pending', 'failed')
                """, (order_id,))
                conn.commit()
                return True
            except Exception as e:
                print(f"Error cancelling message: {e}")
                return False

    def get_stats(self) -> Dict[str, int]:
        """Get queue statistics."""
        with self._lock, sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT status, COUNT(*) as count
                FROM scheduled_messages
                GROUP BY status
            """)

            stats = {"total": 0}
            for row in cursor.fetchall():
                status, count = row
                stats[status] = count
                stats["total"] += count

            return stats

    def cleanup_old_messages(self, days: int = 30) -> int:
        """Remove old sent/failed messages older than specified days."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()

        with self._lock, sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                DELETE FROM scheduled_messages
                WHERE status IN ('sent', 'cancelled')
                  AND created_at < ?
            """, (cutoff,))

            deleted_count = cursor.rowcount
            conn.commit()
            return deleted_count

    def get_message_info(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Get information about scheduled messages for monitoring."""
        with self._lock, sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT order_id, customer_id, order_title, scheduled_time, status, retry_count, last_error
                FROM scheduled_messages
                WHERE status IN ('pending', 'failed')
                  AND retry_count < max_retries
                ORDER BY scheduled_time ASC
                LIMIT ?
            """, (limit,))

            result = []
            for row in cursor.fetchall():
                result.append({
                    "order_id": row[0],
                    "customer_id": row[1],
                    "order_title": row[2],
                    "scheduled_time": row[3],
                    "status": row[4],
                    "retry_count": row[5],
                    "last_error": row[6]
                })

            return result
