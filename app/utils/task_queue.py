#!/usr/bin/env python3
"""SQLite 持久化任务队列 + Worker Pool (2026-06-22 新增)

设计要点:
  - SQLite 单文件持久化，重启可恢复
  - Worker 池：固定 N 线程（= 容器 CPU 配额），原子抢 pending 任务
  - 重启恢复：worker 启动时把所有 status='processing' 任务回滚到 pending
  - 进度回调：handler 通过 queue.update_progress() 写 DB
  - 软取消：queue.cancel_requested 标志 + cancel_event（可选，向后兼容 processing_tasks）

用法:
    queue = TaskQueue('/app/data/tasks.db', worker_count=4)
    queue.register_handler('extract_subtitle', _extract_handler)
    queue.start()
    task_id = queue.add('extract_subtitle', '/path/video.mkv', {'stream_index': 2})
    queue.stop()
"""
import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    video_path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | processing | completed | failed | cancelled
    progress REAL DEFAULT 0,
    progress_info TEXT DEFAULT '',
    payload TEXT,                            -- JSON
    result TEXT,                             -- JSON
    error TEXT,
    cancel_requested INTEGER DEFAULT 0,
    priority INTEGER DEFAULT 0,              -- 2026-06-22 C2: 优先级（数字越大越优先）
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, priority DESC, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_updated ON tasks(updated_at);
"""


class TaskRow:
    """任务记录（轻量 dict-like 包装）"""
    __slots__ = ('id', 'type', 'video_path', 'status', 'progress', 'progress_info',
                 'payload', 'result', 'error', 'cancel_requested',
                 'priority',  # 2026-06-22 C2
                 'created_at', 'updated_at', 'started_at', 'finished_at')

    def __init__(self, row: sqlite3.Row):
        for k in self.__slots__:
            setattr(self, k, row[k] if k in row.keys() else None)

    def to_dict(self, include_payload: bool = True) -> dict:
        d = {k: getattr(self, k) for k in self.__slots__}
        if not include_payload:
            d.pop('payload', None)
        return d


class TaskQueue:
    """SQLite 持久化任务队列"""

    def __init__(self, db_path: str, worker_count: int = 4):
        self.db_path = db_path
        self.worker_count = worker_count
        self.workers: List[threading.Thread] = []
        self.handlers: Dict[str, Callable] = {}
        self._stop_event = threading.Event()
        self._db_lock = threading.Lock()  # SQLite 写串行（SQLite WAL 模式下读可以并发）
        # 2026-06-22 C3: SSE 订阅者列表
        self._subscribers = []  # List[queue.Queue]
        self._subs_lock = threading.Lock()
        self._init_db()

    def subscribe(self):
        """SSE 订阅，返回一个 queue.Queue 用于接收事件"""
        import queue as _queue
        q = _queue.Queue(maxsize=100)
        with self._subs_lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q):
        """SSE 取消订阅"""
        with self._subs_lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def _broadcast(self, event: dict):
        """向所有 SSE 订阅者广播事件（满了就丢，不阻塞 DB 写）"""
        with self._subs_lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(event)
            except Exception:
                # 队列满或异常，跳过（不阻塞主流程）
                pass

    def _init_db(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.executescript(SCHEMA)
            # WAL 模式（提高并发读 + 单写者性能）
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            # 2026-06-22 C2: 已有 tasks 表加 priority 列（如果不存在）
            cur = conn.execute("PRAGMA table_info(tasks)")
            cols = [row[1] for row in cur.fetchall()]
            if 'priority' not in cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN priority INTEGER DEFAULT 0")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, priority DESC, created_at)")
            conn.commit()

    def register_handler(self, task_type: str, handler: Callable):
        """handler(task_id: str, payload: dict, queue: 'TaskQueue') -> None

        handler 内部通过 queue.update_progress(task_id, pct, info) 写进度
        完成后调 queue.complete(task_id, result_dict) 或 queue.fail(task_id, error_str)
        """
        self.handlers[task_type] = handler
        logger.info(f"注册任务 handler: {task_type}")

    def start(self):
        """启动 worker pool + 重启恢复"""
        # 重启恢复：把状态为 processing 的任务回滚到 pending
        self._recover_processing()

        for i in range(self.worker_count):
            t = threading.Thread(target=self._worker_loop, args=(i,), daemon=True, name=f'TQ-Worker-{i}')
            t.start()
            self.workers.append(t)
        logger.info(f"TaskQueue 启动：{self.worker_count} 个 worker")

    def stop(self):
        self._stop_event.set()
        for t in self.workers:
            t.join(timeout=5)
        self.workers.clear()
        logger.info("TaskQueue 停止")

    def _recover_processing(self):
        """把 processing 状态任务回滚到 pending（处理容器重启）"""
        with self._db_lock, sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.execute(
                "UPDATE tasks SET status='cancelled', progress_info='已取消', "
                "finished_at=datetime('now','localtime'), updated_at=datetime('now','localtime') "
                "WHERE status IN ('pending','processing') AND cancel_requested=1"
            )
            cur = conn.execute(
                "UPDATE tasks SET status='pending', progress=0, started_at=NULL, updated_at=datetime('now','localtime') "
                "WHERE status='processing' RETURNING id, type"
            )
            recovered = cur.fetchall()
            conn.commit()
        if recovered:
            logger.warning(f"重启恢复：{len(recovered)} 个 processing 任务回滚到 pending")
            for tid, ttype in recovered:
                logger.info(f"  - {tid} ({ttype})")

    def add(self, task_type: str, video_path: str, payload: Optional[dict] = None, priority: int = 0) -> str:
        """添加新任务，返回 task_id（自动生成）

        Args:
            priority: 任务优先级（默认 0；数字越大越优先；高优任务插队）
        """
        from datetime import datetime
        task_id = f"{task_type}_{os.path.basename(video_path)}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
        with self._db_lock, sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.execute(
                "INSERT INTO tasks (id, type, video_path, status, payload, priority, created_at, updated_at) "
                "VALUES (?, ?, ?, 'pending', ?, ?, datetime('now','localtime'), datetime('now','localtime'))",
                (task_id, task_type, video_path, json.dumps(payload or {}, ensure_ascii=False), int(priority))
            )
            conn.commit()
        logger.info(f"添加任务: {task_id} type={task_type} priority={priority} video={os.path.basename(video_path)}")
        # 2026-06-22 C3: SSE 广播
        self._broadcast({
            'event': 'task_added',
            'task_id': task_id,
            'type': task_type,
            'priority': priority,
            'video_path': video_path,
        })
        return task_id

    def list_batch(self, batch_id):
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM tasks WHERE json_extract(payload, '$.batch_id')=? ORDER BY created_at", (batch_id,)).fetchall()
            return [TaskRow(row) for row in rows]

    def _claim_pending(self) -> Optional[TaskRow]:
        """原子抢一个 pending 任务（UPDATE ... WHERE status='pending' RETURNING）"""
        with self._db_lock, sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "UPDATE tasks SET status='processing', started_at=datetime('now','localtime'), updated_at=datetime('now','localtime') "
                "WHERE id = (SELECT id FROM tasks WHERE status='pending' "
                "  AND cancel_requested=0 ORDER BY priority DESC, created_at LIMIT 1) "
                "RETURNING *"
            )
            row = cur.fetchone()
            conn.commit()
            return TaskRow(row) if row else None

    def _worker_loop(self, worker_id: int):
        """worker 线程主循环"""
        logger.info(f"Worker-{worker_id} 启动")
        while not self._stop_event.is_set():
            try:
                task = self._claim_pending()
                if task is None:
                    # 没有 pending 任务，休眠
                    self._stop_event.wait(timeout=2.0)
                    continue

                logger.info(f"Worker-{worker_id} 抢到任务: {task.id}")
                handler = self.handlers.get(task.type)
                if handler is None:
                    self.fail(task.id, f"未注册 handler: {task.type}")
                    continue

                try:
                    payload = json.loads(task.payload) if task.payload else {}
                    # handler 内部应该调 update_progress() 写进度
                    handler(task.id, payload, self)
                    # 如果 handler 没主动 complete/fail，且没抛异常，当作完成
                    cur_task = self.get(task.id)
                    if cur_task and cur_task.status == 'processing':
                        # 修复 BUG：原直接兜底 complete，但被取消的任务 handler 返回后
                        # 状态仍是 processing，会被误标完成。先检查 cancel_requested。
                        if cur_task.cancel_requested:
                            logger.info(f"Worker-{worker_id} 任务 {task.id} 已取消")
                            with self._db_lock, sqlite3.connect(self.db_path, timeout=30) as conn:
                                conn.execute(
                                    "UPDATE tasks SET status='cancelled', progress_info='已取消', "
                                    "finished_at=datetime('now','localtime'), updated_at=datetime('now','localtime') WHERE id=?",
                                    (task.id,)
                                )
                                conn.commit()
                        else:
                            self.complete(task.id, {'note': 'handler completed without explicit complete()'})
                except Exception as e:
                    logger.exception(f"Worker-{worker_id} 任务 {task.id} 失败")
                    self.fail(task.id, str(e))
            except Exception as e:
                logger.exception(f"Worker-{worker_id} 主循环异常")
                self._stop_event.wait(timeout=2.0)

    # 2026-06-22 D2: 进度节流（同一 task 500ms 内最多 broadcast 1 次 progress；100% 永远 broadcast）
    _last_broadcast = {}  # task_id -> monotonic time

    def update_progress(self, task_id: str, pct: float, info: str = ''):
        """handler 调用写进度"""
        with self._db_lock, sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.execute(
                "UPDATE tasks SET progress=?, progress_info=?, updated_at=datetime('now','localtime') "
                "WHERE id=? AND status='processing'",
                (pct, info, task_id)
            )
            conn.commit()
        # 2026-06-22 C3: SSE 广播 + D2 节流
        import time as _time
        now = _time.monotonic()
        last = self._last_broadcast.get(task_id, 0.0)
        # 100% 永远 broadcast；其他情况 ≥500ms 才 broadcast
        if pct >= 100 or (now - last) >= 0.5:
            self._last_broadcast[task_id] = now
            # 防无界增长：历史任务残留的节流时间戳定期收缩（只影响广播节流精度）
            if len(self._last_broadcast) > 500:
                keep = sorted(self._last_broadcast.items(), key=lambda kv: kv[1])[-250:]
                self._last_broadcast = dict(keep)
            self._broadcast({
                'event': 'task_progress',
                'task_id': task_id,
                'progress': pct,
                'progress_info': info,
            })

    def complete(self, task_id: str, result: dict):
        with self._db_lock, sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.execute(
                "UPDATE tasks SET status='completed', progress=100, progress_info='完成', "
                "result=?, finished_at=datetime('now','localtime'), updated_at=datetime('now','localtime') "
                "WHERE id=? AND status='processing'",
                (json.dumps(result, ensure_ascii=False), task_id)
            )
            conn.commit()
        logger.info(f"任务完成: {task_id}")
        # 2026-06-22 C3: SSE 广播
        self._broadcast({
            'event': 'task_completed',
            'task_id': task_id,
            'result': result,
        })

    def fail(self, task_id: str, error: str):
        with self._db_lock, sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.execute(
                "UPDATE tasks SET status='failed', progress_info=?, error=?, "
                "finished_at=datetime('now','localtime'), updated_at=datetime('now','localtime') "
                "WHERE id=?",
                (f'失败: {error[:80]}', error, task_id)
            )
            conn.commit()
        logger.warning(f"任务失败: {task_id}: {error[:100]}")
        # 2026-06-22 C3: SSE 广播
        self._broadcast({
            'event': 'task_failed',
            'task_id': task_id,
            'error': error[:200],
        })

    def cancel(self, task_id: str) -> bool:
        """软取消：设 cancel_requested=1，handler 应该检查"""
        with self._db_lock, sqlite3.connect(self.db_path, timeout=30) as conn:
            cur = conn.execute(
                "UPDATE tasks SET cancel_requested=1, updated_at=datetime('now','localtime') "
                "WHERE id=? AND status IN ('pending', 'processing')",
                (task_id,)
            )
            conn.commit()
            if cur.rowcount > 0:
                # pending 任务直接取消
                conn.execute(
                    "UPDATE tasks SET status='cancelled', progress_info='已取消', "
                    "finished_at=datetime('now','localtime') WHERE id=? AND status='pending'",
                    (task_id,)
                )
                conn.commit()
                logger.info(f"任务取消: {task_id}")
                return True
        return False

    def retry(self, task_id: str) -> bool:
        """failed/cancelled → pending 重新入队"""
        with self._db_lock, sqlite3.connect(self.db_path, timeout=30) as conn:
            cur = conn.execute(
                "UPDATE tasks SET status='pending', progress=0, progress_info='重试', "
                "error=NULL, cancel_requested=0, started_at=NULL, finished_at=NULL, "
                "updated_at=datetime('now','localtime') "
                "WHERE id=? AND status IN ('failed', 'cancelled')",
                (task_id,)
            )
            conn.commit()
            ok = cur.rowcount > 0
            if ok:
                logger.info(f"任务重试入队: {task_id}")
            return ok

    def delete(self, task_id: str) -> bool:
        with self._db_lock, sqlite3.connect(self.db_path, timeout=30) as conn:
            cur = conn.execute("DELETE FROM tasks WHERE id=? AND status IN ('completed', 'failed', 'cancelled')", (task_id,))
            conn.commit()
            return cur.rowcount > 0

    def is_cancel_requested(self, task_id: str) -> bool:
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            cur = conn.execute("SELECT cancel_requested FROM tasks WHERE id=?", (task_id,))
            row = cur.fetchone()
            return bool(row[0]) if row else False

    def get(self, task_id: str) -> Optional[TaskRow]:
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,))
            row = cur.fetchone()
            return TaskRow(row) if row else None

    def list_tasks(self, status: Optional[str] = None, limit: int = 50) -> List[TaskRow]:
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            if status:
                cur = conn.execute(
                    "SELECT * FROM tasks WHERE status=? ORDER BY updated_at DESC LIMIT ?",
                    (status, limit)
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?",
                    (limit,)
                )
            return [TaskRow(row) for row in cur.fetchall()]

    def get_stats(self) -> dict:
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM tasks GROUP BY status"
            )
            stats = {row['status']: row['cnt'] for row in cur.fetchall()}
            stats['total'] = sum(stats.values())
            stats['active_workers'] = sum(1 for t in self.workers if t.is_alive())
            return stats
