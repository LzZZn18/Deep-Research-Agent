"""带超时与重试的异步调用助手（零依赖）。"""

import asyncio
import logging
from typing import Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def call_with_retry(
    func: Callable[[], Awaitable[T]],
    *,
    retries: int,
    timeout: float,
    retry_on: tuple[type[BaseException], ...],
    base_delay: float = 0.5,
    max_delay: float = 4.0,
) -> T:
    """调用 ``func``，单次超时 ``timeout`` 秒；失败时按指数退避重试。

    - 总尝试次数 = ``retries + 1``（首次 + ``retries`` 次重试）。
    - 退避间隔 = ``min(base_delay * 2^attempt, max_delay)``。
    - 可重试的异常：``asyncio.TimeoutError``（超时）以及 ``retry_on`` 里的异常类型。
    - 重试耗尽后抛出最后一次异常；非重试异常（如 ``asyncio.CancelledError``）直接向上传播。
    """
    last_exc: BaseException | None = None
    for attempt in range(retries + 1):
        try:
            return await asyncio.wait_for(func(), timeout=timeout)
        except asyncio.TimeoutError as e:
            last_exc = e
        except retry_on as e:
            last_exc = e
        if attempt >= retries:
            break
        delay = min(base_delay * (2**attempt), max_delay)
        logger.warning(
            "调用失败（第 %d/%d 次），%.1fs 后重试：%s",
            attempt + 1,
            retries,
            delay,
            last_exc,
        )
        await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc
