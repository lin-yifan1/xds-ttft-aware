from __future__ import annotations

import importlib.util
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
import sys
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "proactive_main", PROJECT_ROOT / "plugin" / "proactive_main.py"
)
proactive = importlib.util.module_from_spec(_spec)
# Register before exec so @dataclass can resolve string annotations (PEP 563).
sys.modules["proactive_main"] = proactive
_spec.loader.exec_module(proactive)


TZ = ZoneInfo("Asia/Shanghai")
CHECKED_AT = datetime(2026, 6, 10, 12, 0, tzinfo=TZ)
MODEL = "deepseek-v3"
SERVICE = "P"


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _row(dt: datetime, domain: str, *, rpm=10.0, tpm=1000.0, ttft=500.0, tpot=30.0,
         prompt=100.0, completion=100.0, success=10.0, error=0.0, **extra) -> dict:
    row = {
        "timestamp": _ms(dt),
        "domain_id": domain,
        "rpm": rpm,
        "tpm": tpm,
        "ttft_avg": ttft,
        "tpot_avg": tpot,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "success_cnt": success,
        "error_cnt": error,
    }
    row.update(extra)
    return row


class ProcessTypeProtocolTest(unittest.TestCase):
    def test_process_type_strings_match_protocol_spelling(self):
        # 协议原文拼写（公司接口/DB 一致），含 compeletion 笔误，不可“修正”。
        self.assertEqual(proactive.PROCESS_RPM_LIMIT, "rpm_limit")
        self.assertEqual(proactive.PROCESS_TPM_LIMIT, "tpm_limit")
        self.assertEqual(proactive.PROCESS_COMPLETION_LIMIT, "compeletion_token_limit")

    def test_score_weights_are_both_and_normalized(self):
        self.assertEqual(len(proactive.SCORE_WEIGHTS), 3)
        self.assertAlmostEqual(sum(proactive.SCORE_WEIGHTS), 1.0, places=3)


class SlaTableTest(unittest.TestCase):
    def test_glm_model_uses_glm_sla(self):
        cfg = proactive.PluginConfig()
        ttft, tpot, source = proactive.resolve_sla("GLM-4-Plus", cfg)
        self.assertEqual((ttft, tpot), (30000.0, 500.0))
        self.assertEqual(source, "model_table:glm")

    def test_other_model_uses_default_sla(self):
        cfg = proactive.PluginConfig()
        ttft, tpot, source = proactive.resolve_sla(MODEL, cfg)
        self.assertEqual((ttft, tpot), (10000.0, 150.0))
        self.assertEqual(source, "model_table:default")

    def test_env_override_wins(self):
        cfg = proactive.PluginConfig(ttft_sla_override=15000.0, tpot_sla_override=50.0)
        ttft, tpot, source = proactive.resolve_sla("GLM-4", cfg)
        self.assertEqual((ttft, tpot), (15000.0, 50.0))
        self.assertEqual(source, "env_override")


class ClassifyScenarioTest(unittest.TestCase):
    def setUp(self):
        self.cfg = proactive.PluginConfig()  # trigger 1.3, margin 1.25

    @staticmethod
    def _baseline():
        return {"rpm": 100.0, "tpm": 200000.0, "prompt_tokens": 800.0,
                "completion_tokens": 300.0}

    def _classify(self, rpm=100.0, tpm=200000.0, completion=300.0, baseline=None):
        current = {"rpm": rpm, "tpm": tpm, "completion_tokens": completion}
        return proactive.classify_scenario(
            self.cfg, current, baseline or self._baseline()
        )

    def test_single_rpm_trigger(self):
        scenario, ratios, triggered, suppressed = self._classify(rpm=200.0)
        self.assertEqual(scenario["type"], "rpm_rise_dominant")
        self.assertEqual(scenario["process_type"], "rpm_limit")
        self.assertEqual(scenario["decision"], "single")
        self.assertEqual(triggered, ["rpm"])
        self.assertEqual(suppressed, [])

    def test_single_tpm_trigger(self):
        scenario, _, _, _ = self._classify(tpm=400000.0)
        self.assertEqual(scenario["type"], "tpm_rise_dominant")
        self.assertEqual(scenario["process_type"], "tpm_limit")

    def test_single_output_trigger(self):
        scenario, _, _, _ = self._classify(completion=600.0)
        self.assertEqual(scenario["type"], "output_shift_dominant")
        self.assertEqual(scenario["process_type"], "compeletion_token_limit")

    def test_output_dominant_over_tpm_via_margin(self):
        # 输出激增 ratio 2.0 连带 tpm 1.4：2.0 >= 1.4*1.25=1.75 -> output dominant
        scenario, _, triggered, _ = self._classify(tpm=280000.0, completion=600.0)
        self.assertEqual(set(triggered), {"tpm", "completion_tokens"})
        self.assertEqual(scenario["type"], "output_shift_dominant")
        self.assertEqual(scenario["decision"], "margin")

    def test_close_ratios_fall_to_default(self):
        # rpm 2.0 + tpm 2.0：并列，margin 不满足 -> default(mixed) -> rpm_limit
        scenario, _, _, _ = self._classify(rpm=200.0, tpm=400000.0)
        self.assertEqual(scenario["type"], "default")
        self.assertEqual(scenario["process_type"], "rpm_limit")
        self.assertEqual(scenario["decision"], "mixed")

    def test_margin_not_met_falls_to_default(self):
        # completion 1.5 + tpm 1.4：1.5 < 1.4*1.25=1.75 -> default
        scenario, _, _, _ = self._classify(tpm=280000.0, completion=450.0)
        self.assertEqual(scenario["type"], "default")

    def test_no_trigger_returns_none(self):
        scenario, ratios, triggered, _ = self._classify(rpm=120.0)
        self.assertIsNone(scenario)
        self.assertEqual(triggered, [])
        self.assertAlmostEqual(ratios["rpm"], 1.2)

    def test_missing_baseline_suppressed(self):
        baseline = self._baseline()
        baseline["tpm"] = None
        scenario, ratios, _, suppressed = self._classify(
            rpm=200.0, tpm=999999.0, baseline=baseline
        )
        self.assertEqual(scenario["type"], "rpm_rise_dominant")
        self.assertEqual(suppressed, ["tpm"])
        self.assertNotIn("tpm", ratios)

    def test_trigger_boundary_inclusive(self):
        scenario, _, _, _ = self._classify(rpm=130.0)
        self.assertIsNotNone(scenario)
        self.assertEqual(scenario["type"], "rpm_rise_dominant")


class PoolLeverTest(unittest.TestCase):
    def setUp(self):
        self.cfg = proactive.PluginConfig()  # rpm 0.8, tpm 1.5, output 1.5

    def test_rpm_lever_scale(self):
        lever = proactive._lever_for_metric(
            self.cfg, "rpm", {"rpm": 100.0}, {"rpm": 62.5}
        )
        self.assertEqual(lever["kind"], "rate_scale")
        self.assertAlmostEqual(lever["target"], 50.0)
        self.assertAlmostEqual(lever["s"], 0.5)

    def test_tpm_band_not_reducible(self):
        # 触发区间 [1.3, 1.5) 内 s>=1：current=1.4*baseline，target=1.5*baseline
        lever = proactive._lever_for_metric(
            self.cfg, "tpm", {"tpm": 1400.0}, {"tpm": 1000.0}
        )
        self.assertIsNone(lever)

    def test_completion_cap_always_computable(self):
        lever = proactive._lever_for_metric(
            self.cfg, "completion_tokens", {"completion_tokens": 400.0},
            {"completion_tokens": 300.0},
        )
        self.assertEqual(lever["kind"], "length_cap")
        self.assertAlmostEqual(lever["cap"], 450.0)

    def test_dominant_scenario_no_fallback(self):
        scenario = {"type": "tpm_rise_dominant", "process_type": "tpm_limit"}
        lever, ptype, note, warning = proactive.compute_pool_lever(
            self.cfg, scenario,
            {"rpm": 200.0, "tpm": 1400.0}, {"rpm": 100.0, "tpm": 1000.0},
            {"tpm": 1.4}, ["tpm"],
        )
        self.assertIsNone(lever)
        self.assertEqual(ptype, "tpm_limit")
        self.assertIn("lever_not_computable", note)

    def test_default_falls_back_when_rpm_baseline_missing(self):
        scenario = {"type": "default", "process_type": "rpm_limit"}
        lever, ptype, note, warning = proactive.compute_pool_lever(
            self.cfg, scenario,
            {"rpm": 100.0, "tpm": 2000.0, "completion_tokens": 500.0},
            {"rpm": None, "tpm": 1000.0, "completion_tokens": 300.0},
            {"tpm": 2.0, "completion_tokens": 1.67}, ["tpm", "completion_tokens"],
        )
        self.assertIsNotNone(lever)
        self.assertEqual(lever["metric"], "tpm")
        self.assertEqual(ptype, "tpm_limit")
        self.assertIn("default_fallback_to_tpm_limit", warning)

    def test_default_falls_back_when_rpm_not_reducible(self):
        # rpm current 已低于 0.8*baseline -> s>=1，兜底到 completion
        scenario = {"type": "default", "process_type": "rpm_limit"}
        lever, ptype, note, warning = proactive.compute_pool_lever(
            self.cfg, scenario,
            {"rpm": 70.0, "tpm": 0.0, "completion_tokens": 500.0},
            {"rpm": 100.0, "tpm": None, "completion_tokens": 300.0},
            {"completion_tokens": 1.67}, ["completion_tokens"],
        )
        self.assertIsNotNone(lever)
        self.assertEqual(lever["metric"], "completion_tokens")
        self.assertEqual(ptype, "compeletion_token_limit")


class ActiveEventTest(unittest.TestCase):
    def setUp(self):
        self.cfg = proactive.PluginConfig()  # active_recent_minutes=5
        self.ttft = np.zeros(60)

    def test_event_touching_tail_is_active(self):
        ev = proactive._select_active_event(
            self.cfg, [(40, 59)], 60, self.ttft, 10000.0
        )
        self.assertEqual(ev, (40, 59))

    def test_event_at_threshold_is_active(self):
        ev = proactive._select_active_event(
            self.cfg, [(40, 55)], 60, self.ttft, 10000.0
        )
        self.assertEqual(ev, (40, 55))

    def test_stale_event_is_ignored(self):
        ev = proactive._select_active_event(
            self.cfg, [(10, 30)], 60, self.ttft, 10000.0
        )
        self.assertIsNone(ev)

    def test_latest_end_wins(self):
        ev = proactive._select_active_event(
            self.cfg, [(40, 56), (50, 59)], 60, self.ttft, 10000.0
        )
        self.assertEqual(ev, (50, 59))


class TpotFlagTest(unittest.TestCase):
    def test_tpot_disabled_by_default(self):
        cfg = proactive.PluginConfig()
        ttft = np.zeros(20)
        tpot = np.full(20, 9999.0)  # 远超 SLA
        info = proactive._detect_system_events(cfg, ttft, tpot, 10000.0, 150.0)
        self.assertEqual(info["events"], [])

    def test_tpot_enabled_detects(self):
        cfg = proactive.PluginConfig(enable_tpot=True)
        ttft = np.zeros(20)
        tpot = np.full(20, 9999.0)
        info = proactive._detect_system_events(cfg, ttft, tpot, 10000.0, 150.0)
        self.assertEqual(info["events"], [(0, 19)])
        self.assertEqual(
            proactive._scope_for_window(
                info["sys_anom_ttft"], info["sys_anom_tpot"], 0, 19
            ),
            "tpot_only",
        )


class RegionBreakdownTest(unittest.TestCase):
    def _r3df(self):
        rows = []
        base = CHECKED_AT - timedelta(minutes=20)
        for i in range(20):
            dt = base + timedelta(minutes=i)
            rows.append(_row(dt, "A", rpm=60.0, project_id="p1",
                             resident_model_id="g1", region="贵阳",
                             infer_service_id=SERVICE))
            rows.append(_row(dt, "A", rpm=40.0, project_id="p1",
                             resident_model_id="g1", region="贵阳",
                             infer_service_id="P2"))
            rows.append(_row(dt, "A", rpm=50.0, project_id="p2",
                             resident_model_id="g2", region="香港",
                             infer_service_id="P3"))
        return proactive.rows_to_dataframe(
            rows, "Asia/Shanghai",
            extra_str_cols=("project_id", "resident_model_id", "region", "infer_service_id"),
        )

    def test_fanout_excludes_resident_not_routing_to_pool(self):
        lever = {"metric": "rpm", "kind": "rate_scale", "s": 0.08}
        breakdown, note = proactive.build_region_breakdown(
            self._r3df(), "A", SERVICE, lever
        )
        self.assertIsNone(note)
        self.assertEqual(len(breakdown), 1)  # g2/香港 不路由到 P，被排除
        entry = breakdown[0]
        self.assertEqual(entry["resident_model_id"], "g1")
        self.assertEqual(entry["region"], "贵阳")
        # region_total = 跨池逐分钟求和 (60+40)=100 的非零均值
        self.assertAlmostEqual(entry["region_total"], 100.0)
        self.assertEqual(entry["value"], 8)  # floor(100*0.08)
        self.assertEqual(entry["project_id"], "p1")

    def test_length_cap_same_value_no_amplify(self):
        lever = {"metric": "completion_tokens", "kind": "length_cap", "cap": 450.7}
        breakdown, note = proactive.build_region_breakdown(
            self._r3df(), "A", SERVICE, lever
        )
        self.assertIsNone(note)
        self.assertEqual(breakdown[0]["value"], 450)
        self.assertNotIn("region_total", breakdown[0])

    def test_unknown_domain_gives_note(self):
        lever = {"metric": "rpm", "kind": "rate_scale", "s": 0.5}
        breakdown, note = proactive.build_region_breakdown(
            self._r3df(), "ZZZ", SERVICE, lever
        )
        self.assertEqual(breakdown, [])
        self.assertEqual(note, "resident_breakdown_unavailable")

    def test_value_floor_min_one(self):
        lever = {"metric": "rpm", "kind": "rate_scale", "s": 0.001}
        breakdown, _ = proactive.build_region_breakdown(
            self._r3df(), "A", SERVICE, lever
        )
        self.assertEqual(breakdown[0]["value"], 1)  # floor(0.1) -> 钳到 1


class RetryTest(unittest.TestCase):
    def _client(self):
        return proactive.MaasClient(
            url="https://example/api", appcode="c", apply_domain_id="d",
            apply_project_id="p", retry_max=3, retry_base_seconds=2.0,
        )

    def test_429_retries_then_succeeds(self):
        ok_body = {"code": 200, "list": [], "pages": 1, "pageNum": 1}
        responses = [
            mock.Mock(status_code=429, text="rate limited"),
            mock.Mock(status_code=429, text="rate limited"),
            mock.Mock(status_code=200, json=mock.Mock(return_value=ok_body)),
        ]
        with mock.patch.object(proactive.requests, "post", side_effect=responses) as post, \
                mock.patch.object(proactive.time, "sleep") as sleep:
            client = self._client()
            body = client._post({"q": 1})
        self.assertEqual(body["code"], 200)
        self.assertEqual(post.call_count, 3)
        self.assertEqual(client.http_call_count, 3)
        sleep.assert_has_calls([mock.call(2.0), mock.call(4.0)])

    def test_429_exhausted_raises(self):
        responses = [mock.Mock(status_code=429, text="rate limited")] * 4
        with mock.patch.object(proactive.requests, "post", side_effect=responses), \
                mock.patch.object(proactive.time, "sleep"):
            client = self._client()
            with self.assertRaises(proactive.MaasApiError) as ctx:
                client._post({"q": 1})
        self.assertEqual(ctx.exception.status, 429)


class EndToEndTest(unittest.TestCase):
    """打桩 MaasClient.query 跑通 R1 -> 检测 -> R2 -> 场景 -> R3 -> strategies。"""

    @staticmethod
    def _r1_rows():
        rows = []
        win_start = CHECKED_AT - timedelta(minutes=60)
        for i in range(60):
            dt = win_start + timedelta(minutes=i)
            # B：全窗口的健康背景租户
            rows.append(_row(dt, "B", rpm=5.0, tpm=500.0, ttft=500.0,
                             prompt=50.0, completion=50.0))
            # A：最后 20 分钟 rpm 激增 + TTFT 重度击穿（80000 >= 10000*7）
            if i >= 40:
                rows.append(_row(dt, "A", rpm=100.0, tpm=1100.0, ttft=80000.0,
                                 prompt=100.0, completion=100.0))
        return rows

    @staticmethod
    def _r2_rows():
        rows = []
        for day in range(1, 8):  # 7 天历史，满足 min_baseline_points=6
            for i in range(40, 60):
                dt = CHECKED_AT - timedelta(days=day, minutes=60) + timedelta(minutes=i)
                rows.append(_row(dt, "A", rpm=10.0, tpm=1000.0, ttft=500.0,
                                 prompt=100.0, completion=100.0))
                rows.append(_row(dt, "B", rpm=5.0, tpm=500.0, ttft=400.0,
                                 prompt=50.0, completion=50.0))
        return rows

    @staticmethod
    def _r3_rows():
        rows = []
        for i in range(40, 60):
            dt = CHECKED_AT - timedelta(minutes=60) + timedelta(minutes=i)
            rows.append(_row(dt, "A", rpm=60.0, project_id="p1",
                             resident_model_id="g1", region="贵阳",
                             infer_service_id=SERVICE))
            rows.append(_row(dt, "A", rpm=40.0, project_id="p1",
                             resident_model_id="g1", region="贵阳",
                             infer_service_id="P2"))
            rows.append(_row(dt, "A", rpm=50.0, project_id="p1",
                             resident_model_id="g2", region="香港",
                             infer_service_id="P3"))
        return rows

    def _run(self, cfg=None):
        calls = []
        test = self

        def fake_query(self_client, filters, dimensions=None):
            calls.append({"filters": filters, "dimensions": dimensions})
            if dimensions is not None and len(dimensions) == len(proactive.R3_DIMENSIONS):
                return test._r3_rows()
            if any(f.get("name") == "infer_service_id" for f in filters):
                return test._r1_rows()
            return test._r2_rows()

        with mock.patch.object(proactive.MaasClient, "query", new=fake_query):
            result, exit_code = proactive.run_plugin(
                SERVICE, MODEL, CHECKED_AT.isoformat(), "https://example/api",
                "code", "dom", "proj", cfg=cfg or proactive.PluginConfig(),
            )
        return result, exit_code, calls

    def test_full_pipeline_emits_strategy(self):
        result, exit_code, calls = self._run()
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "anomaly")
        self.assertEqual(result["mode"], "proactive")
        self.assertEqual(len(calls), 3)  # R1 + R2 + R3

        self.assertEqual(len(result["events"]), 1)
        event = result["events"][0]
        self.assertEqual(event["scope"], "ttft_only")
        self.assertEqual(event["duration_minutes"], 20)

        self.assertEqual(len(result["culprits"]), 1)
        culprit = result["culprits"][0]
        self.assertEqual(culprit["domain_id"], "A")
        self.assertEqual(culprit["scenario"]["type"], "rpm_rise_dominant")
        self.assertEqual(culprit["scenario"]["decision"], "single")
        self.assertAlmostEqual(culprit["pool_lever"]["s"], 0.08)

        self.assertEqual(
            result["strategies"],
            [
                {
                    "domain_id": "A",
                    "resident_model_id": "g1",
                    "region": "贵阳",
                    "process_type": "rpm_limit",
                    "value": 8,
                    "model_name": MODEL,
                    "project_id": "p1",
                    "scenario": "rpm_rise_dominant",
                }
            ],
        )

    def test_detect_only_skips_round2_and_3(self):
        cfg = replace(proactive.PluginConfig(), detect_only=True)
        result, exit_code, calls = self._run(cfg=cfg)
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "anomaly")
        self.assertEqual(result["note"], "detect_only")
        self.assertEqual(result["culprits"], [])
        self.assertEqual(result["strategies"], [])
        self.assertEqual(len(calls), 1)  # 仅 R1

    def test_stale_event_reports_normal(self):
        test = self

        def fake_query(self_client, filters, dimensions=None):
            # 过载只发生在窗口第 5~25 分钟，距末端 > 5 分钟 -> 不活跃
            rows = []
            win_start = CHECKED_AT - timedelta(minutes=60)
            for i in range(60):
                dt = win_start + timedelta(minutes=i)
                rows.append(_row(dt, "B", rpm=5.0, ttft=500.0))
                if 5 <= i <= 25:
                    rows.append(_row(dt, "A", rpm=100.0, ttft=80000.0))
            return rows

        with mock.patch.object(proactive.MaasClient, "query", new=fake_query):
            result, exit_code = proactive.run_plugin(
                SERVICE, MODEL, CHECKED_AT.isoformat(), "https://example/api",
                "code", "dom", "proj", cfg=proactive.PluginConfig(),
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "normal")
        self.assertEqual(result["inactive_event_count"], 1)
        self.assertEqual(result["strategies"], [])

    def test_no_data(self):
        def fake_query(self_client, filters, dimensions=None):
            return []

        with mock.patch.object(proactive.MaasClient, "query", new=fake_query):
            result, exit_code = proactive.run_plugin(
                SERVICE, MODEL, CHECKED_AT.isoformat(), "https://example/api",
                "code", "dom", "proj", cfg=proactive.PluginConfig(),
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "no_data")

    def test_round_queries_carry_model_filter(self):
        _, _, calls = self._run()
        for call in calls:
            names = {f["name"] for f in call["filters"]}
            self.assertIn("model_name", names)


class ArgContractTest(unittest.TestCase):
    def test_arg_names_and_count(self):
        self.assertEqual(proactive.EXPECTED_ARG_COUNT, 7)
        self.assertEqual(
            proactive.ARG_NAMES,
            ("service_id", "model_name", "time", "maasApiurl",
             "appcode", "applydomainid", "applyprojectid"),
        )

    def test_main_rejects_wrong_arg_count(self):
        self.assertEqual(proactive.main(["only", "three", "args"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
