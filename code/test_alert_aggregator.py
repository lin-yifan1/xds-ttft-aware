"""AlertAggregator 的并发与缓存行为测试。

只用标准库 unittest，避免引入新的测试依赖。运行::

    uv run python -m unittest code.test_alert_aggregator
"""

from __future__ import annotations

import threading
import time
import unittest
from datetime import datetime, timezone

from alert_aggregator import AlertAggregator, AlertRequest


def _make_alert(
    minute: int = 0,
    service: str = "svc-001",
    model: str | None = "qwen-2.5",
    second: int = 0,
) -> AlertRequest:
    """构造一条 2026-05-19 10:{minute}:{second} 的告警。"""
    return AlertRequest(
        reported_at=datetime(2026, 5, 19, 10, minute, second, tzinfo=timezone.utc),
        infer_service_id=service,
        model_name=model,
    )


class AlertAggregatorTests(unittest.TestCase):

    def test_same_minute_dedups_to_single_handler_call(self) -> None:
        """同 key 的告警只会让 handler 执行一次。"""
        calls = []
        call_lock = threading.Lock()
        gate = threading.Event()

        def handler(alert: AlertRequest) -> dict:
            with call_lock:
                calls.append(alert)
            # 等待外部释放，模拟一次稍长的 API 调用
            gate.wait(timeout=2.0)
            return {"status": "normal", "service": alert.infer_service_id}

        agg = AlertAggregator(handler=handler, result_ttl_seconds=0, max_workers=4)
        try:
            results = [None, None, None]

            def submit(i: int, alert: AlertRequest) -> None:
                results[i] = agg.submit(alert)

            threads = [
                threading.Thread(target=submit, args=(i, _make_alert(second=i * 5)))
                for i in range(3)
            ]
            for t in threads:
                t.start()
            # 等三个 submit 都进入 wait
            time.sleep(0.1)
            gate.set()
            for t in threads:
                t.join(timeout=3)

            self.assertEqual(len(calls), 1, "handler 应只被调用一次")
            for r in results:
                self.assertIsNotNone(r)
                self.assertEqual(r["status"], "normal")
                self.assertIn("aggregation", r)
            # 其中 2 个应该是复用的
            reused_flags = [r["aggregation"]["reused"] for r in results]
            self.assertEqual(reused_flags.count(False), 1)
            self.assertEqual(reused_flags.count(True), 2)

            stats = agg.snapshot_stats()
            self.assertEqual(stats["total_submitted"], 3)
            self.assertEqual(stats["unique_handler_calls"], 1)
            self.assertEqual(stats["deduped_inflight"], 2)
        finally:
            agg.shutdown()

    def test_different_keys_trigger_separate_calls(self) -> None:
        calls: list[AlertRequest] = []
        lock = threading.Lock()

        def handler(alert: AlertRequest) -> dict:
            with lock:
                calls.append(alert)
            return {"status": "normal"}

        agg = AlertAggregator(handler=handler, result_ttl_seconds=0)
        try:
            agg.submit(_make_alert(minute=0, service="svc-001"))
            agg.submit(_make_alert(minute=0, service="svc-002"))
            agg.submit(_make_alert(minute=1, service="svc-001"))
            agg.submit(_make_alert(minute=0, service="svc-001", model="other"))
            self.assertEqual(len(calls), 4)
        finally:
            agg.shutdown()

    def test_ttl_cache_returns_same_result_without_recall(self) -> None:
        counter = {"n": 0}

        def handler(alert: AlertRequest) -> dict:
            counter["n"] += 1
            return {"status": "normal", "n": counter["n"]}

        # 用虚拟时钟控制 TTL 评估
        now = {"t": 1000.0}

        def clock() -> float:
            return now["t"]

        agg = AlertAggregator(
            handler=handler,
            result_ttl_seconds=10.0,
            max_workers=2,
            clock=clock,
        )
        try:
            r1 = agg.submit(_make_alert())
            self.assertEqual(r1["n"], 1)
            self.assertFalse(r1["aggregation"]["reused"])

            # 在 TTL 内再来一次，命中缓存
            now["t"] += 5.0
            r2 = agg.submit(_make_alert(second=30))  # 同分钟、同 key
            self.assertEqual(r2["n"], 1, "TTL 内复用同一结果")
            self.assertTrue(r2["aggregation"]["reused"])
            self.assertEqual(counter["n"], 1)

            # 推进时钟越过 TTL，重新触发 handler
            now["t"] += 100.0
            r3 = agg.submit(_make_alert(second=45))
            self.assertEqual(r3["n"], 2)
            self.assertFalse(r3["aggregation"]["reused"])
            self.assertEqual(counter["n"], 2)
        finally:
            agg.shutdown()

    def test_handler_failure_does_not_poison_cache(self) -> None:
        attempts = {"n": 0}

        def handler(alert: AlertRequest) -> dict:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("boom")
            return {"status": "normal"}

        agg = AlertAggregator(handler=handler, result_ttl_seconds=60.0)
        try:
            r1 = agg.submit(_make_alert())
            self.assertEqual(r1["status"], "error")
            self.assertEqual(r1["error_type"], "RuntimeError")
            self.assertIn("boom", r1["error"])

            # 失败后立即重试应再次触发 handler，而不是返回缓存的错误
            r2 = agg.submit(_make_alert(second=30))
            self.assertEqual(r2["status"], "normal")
            self.assertEqual(attempts["n"], 2)
        finally:
            agg.shutdown()

    def test_key_normalizes_seconds_and_optional_model(self) -> None:
        a = _make_alert(second=0, model=None)
        b = _make_alert(second=45, model=None)
        c = _make_alert(second=10, model="qwen-2.5")
        key_a = AlertAggregator.make_key(a)
        key_b = AlertAggregator.make_key(b)
        key_c = AlertAggregator.make_key(c)
        self.assertEqual(key_a, key_b)
        self.assertNotEqual(key_a, key_c)
        self.assertEqual(key_a[1], "")  # model_name=None 归一化为空串


if __name__ == "__main__":
    unittest.main()
