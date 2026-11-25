import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Optional

from loguru import logger


@dataclass(order=True)
class PrioritizedQueryItemBase:
    priority: Optional[int] = 0
    _index: Optional[int] = 0
    data: Any = None
    callback: Optional[Callable] = None


class PoolBase:
    def __init__(
        self, max_num: int, max_sleep: Optional[int] = 30, min_sleep: Optional[int] = 15
    ) -> None:
        self.semaphore = asyncio.Semaphore(max_num)
        self.max_sleep = max_sleep
        self.min_sleep = min_sleep
        self.index = 0
        self.loop_num = 0
        self.queue = asyncio.PriorityQueue()

    def init(self):
        asyncio.create_task(self.task_loop())

    async def task_loop(self):
        self.loop_num += 1
        loop_num = self.loop_num
        while self.loop_num == loop_num:
            try:
                item = await self.queue.get()
                await self.semaphore.acquire()
                asyncio.create_task(self._do_single(item))
            except Exception as e:
                logger.error(str(e))

    async def _do_single(self, item: PrioritizedQueryItemBase):
        try:
            self.queue.task_done()
            await self.do_single(item)
        except Exception as e:
            logger.error(str(e))
        finally:
            self.semaphore.release()

    async def add_task(
        self,
        item: PrioritizedQueryItemBase,
        runtime: int = 0,
        log: bool = False,
        label: str = "",
    ):
        try:
            if runtime:
                await self.auto_sleep(runtime, log, label)
            item._index = self.index
            await self.queue.put(item)
            self.index = (self.index + 1) % 9999999
        except Exception as e:
            logger.error(f"{label}添加任务失败：{str(e)}")

    async def auto_sleep(self, run_time: int, log: bool = False, label: str = ""):
        if run_time > self.max_sleep:
            # 常态轮次（本轮很快、补等到最小间隔）是稳态，不记录日志：
            # loguru 默认 sink 是 DEBUG，写成 logger.debug 也照样每 15s 刷一条。
            # 只有明显变慢（过慢）才值得告警。
            if log:
                logger.warning(f"{label}运行时间{run_time}, 过慢，不等待")
        elif run_time < self.min_sleep:
            await asyncio.sleep(self.min_sleep - run_time)

    async def do_single(self, item: PrioritizedQueryItemBase):
        raise NotImplementedError
