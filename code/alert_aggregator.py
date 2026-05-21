"""告警聚合器。

外部告警系统经常在短时间内发出针对同一推理服务、同一上报分钟的多条
告警（例如 SDK 重试、不同探针同时上报、不同维度阈值各自触发等）。
逐条调用 ``analyze_cluster`` 会重复打 MaaS Monitor API，浪费配额。

本模块把告警按 ``(infer_service_id, model_name, reported_at 分钟)``
归并：

* In-flight 复用：同 key 的 Future 还在执行时，后到的告警直接 ``wait``
  同一个 Future，不会再发起 API 查询。
* TTL 缓存复用：Future 完成后在 ``result_ttl_seconds`` 内仍然驻留，
  期间命中同 key 的告警直接返回缓存结果。
* 失败不缓存：handler 抛异常时立即把 entry 移出表，避免坏结果反复污染。

聚合器本身不关心 handler 的内部实现，便于在 ``alert_service.py`` 中替换
``analyze_cluster`` 也便于在测试中替换为 mock。
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AlertRequest:
    """单条告警的归一化表示。

    ``reported_at`` 已经被调用方 floor 到分钟级；保留 ``tzinfo`` 以便
    handler 内部使用。``model_name`` 允许为 None。
    """

    reported_at: datetime
    infer_service_id: str
    model_name: Optional[str] = None
    raw: Optional[dict[str, Any]] = None  # 透传原始 payload，便于追踪


@dataclass
class _Entry:
    future: Future
    created_at: float


@dataclass
class AggregatorStats:
    total_submitted: int = 0
    unique_handler_calls: int = 0
    deduped_inflight: int = 0
    deduped_cached: int = 0
    failed_handler_calls: int = 0
    expired_evictions: int = 0
    handler_call_keys: list[str] = field(default_factory=list)  # 仅供调试


class AlertAggregator:
    """线程安全的告警聚合器。

    Parameters
    ----------
    handler:
        真正执行检测的回调；接收 :class:`AlertRequest`，返回 JSON 可序列化 dict。
    result_ttl_seconds:
        Future 完成后保留多久（秒）。期间命中同 key 的告警直接返回缓存。
        设为 0 时关闭 TTL 缓存（只保留 in-flight 复用）。
    max_workers:
        ``ThreadPoolExecutor`` 的线程数。
    clock:
        测试用的时间源，默认 ``time.monotonic``。
    """

    def __init__(
        self,
        handler: Callable[[AlertRequest], dict[str, Any]],
        result_ttl_seconds: float = 60.0,
        max_workers: int = 4,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._handler = handler
        self._ttl = max(float(result_ttl_seconds), 0.0)
        self._lock = threading.Lock()
        self._entries: dict[tuple, _Entry] = {}
        self._executor = ThreadPoolExecutor(
            max_workers=int(max_workers),
            thread_name_prefix="alert-agg",
        )
        self._clock = clock
        self._stats = AggregatorStats()

    # ---- key & helpers ----------------------------------------------------

    @staticmethod
    def make_key(alert: AlertRequest) -> tuple[str, str, str]:
        """按 (infer_service_id, model_name, reported_at 分钟 ISO) 计算 key。

        ``reported_at`` 在被聚合器接收前一般已被 ``parse_reported_at`` 截到分钟，
        这里再做一次 ``replace(second=0, microsecond=0)`` 兜底，避免调用方
        忘记截断时把 key 拆散。
        """
        floored = alert.reported_at.replace(second=0, microsecond=0)
        return (
            str(alert.infer_service_id),
            alert.model_name or "",
            floored.isoformat(),
        )

    # ---- public API -------------------------------------------------------

    def submit(
        self,
        alert: AlertRequest,
        wait_timeout: Optional[float] = None,
    ) -> dict[str, Any]:
        """提交告警，同步等待并返回检测结果。

        线程安全。失败时把异常包装为可序列化 JSON ``{"status": "error", ...}``。
        """
        key = self.make_key(alert)
        future, reused = self._get_or_create_future(alert, key)
        try:
            result = future.result(timeout=wait_timeout)
        except Exception as exc:  # 包含超时、handler 异常
            with self._lock:
                self._stats.failed_handler_calls += 1
                # handler 失败时不应留作缓存
                entry = self._entries.get(key)
                if entry is not None and entry.future is future:
                    self._entries.pop(key, None)
            logger.exception(
                "alert handler failed reused=%s key=%s", reused, key
            )
            return {
                "status": "error",
                "error": str(exc),
                "error_type": exc.__class__.__name__,
                "aggregation": {"key": list(key), "reused": reused},
            }

        # 浅拷贝避免外部修改污染缓存
        out = dict(result) if isinstance(result, dict) else {"result": result}
        out.setdefault("aggregation", {})
        out["aggregation"]["key"] = list(key)
        out["aggregation"]["reused"] = reused
        return out

    def snapshot_stats(self) -> dict[str, int]:
        with self._lock:
            entries = self._entries.values()
            pending = sum(1 for e in entries if not e.future.done())
            cached = sum(1 for e in entries if e.future.done())
            return {
                "total_submitted": self._stats.total_submitted,
                "unique_handler_calls": self._stats.unique_handler_calls,
                "deduped_inflight": self._stats.deduped_inflight,
                "deduped_cached": self._stats.deduped_cached,
                "failed_handler_calls": self._stats.failed_handler_calls,
                "expired_evictions": self._stats.expired_evictions,
                "pending_keys": pending,
                "cached_keys": cached,
            }

    def shutdown(self, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait)

    # ---- internals --------------------------------------------------------

    def _get_or_create_future(
        self,
        alert: AlertRequest,
        key: tuple[str, str, str],
    ) -> tuple[Future, bool]:
        """根据 key 找出可复用的 Future，或新建一个。

        返回 ``(future, reused)``。``reused=True`` 表示该 Future 已存在
        （可能 in-flight 或在 TTL 缓存内）。
        """
        now = self._clock()
        with self._lock:
            self._stats.total_submitted += 1
            entry = self._entries.get(key)
            if entry is not None:
                if entry.future.done() and self._ttl > 0 and now - entry.created_at <= self._ttl:
                    self._stats.deduped_cached += 1
                    return entry.future, True
                if not entry.future.done():
                    self._stats.deduped_inflight += 1
                    return entry.future, True
                # 已完成但过期；或者 TTL=0
                self._stats.expired_evictions += 1
                self._entries.pop(key, None)

            future: Future = self._executor.submit(self._run_safe, alert)
            self._entries[key] = _Entry(future=future, created_at=now)
            self._stats.unique_handler_calls += 1
            self._stats.handler_call_keys.append("|".join(key))
            return future, False

    def _run_safe(self, alert: AlertRequest) -> dict[str, Any]:
        # handler 内部异常会被 Future 捕获再透出到 submit 的 try/except
        return self._handler(alert)
