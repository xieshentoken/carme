"""事件总线 —— 把「bot 正在干什么」实时推给前端。

用最朴素的 asyncio.Queue 广播：每个 SSE 连接一个队列，
有事件就扇出。个人用量级下不需要 Redis 或消息队列。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

log = logging.getLogger("carme.bus")


class EventBus:
    def __init__(self, history_limit: int = 300) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._history: list[dict] = []
        self._history_limit = history_limit
        self._lock = asyncio.Lock()

    async def subscribe(self, *, replay: bool = True) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        async with self._lock:
            self._subscribers.add(queue)
        # 新连接先把最近的历史灌进去，界面一打开就有上下文
        for event in list(self._history) if replay else []:
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue) -> None:
        async with self._lock:
            self._subscribers.discard(queue)

    async def publish(self, event: dict[str, Any]) -> None:
        self._history.append(event)
        if len(self._history) > self._history_limit:
            self._history = self._history[-self._history_limit :]

        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # 慢消费者直接丢最旧的，不阻塞生产者
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(event)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
