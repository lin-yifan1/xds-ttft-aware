# scenario: ttft_only_new_user

> 由 `tests/scenario_ttft_only_new_user.py` 重构而来；历史见 git log。

通过构造 API 返回数据探测异常检测算法 —— **TTFT-only + 新用户加入** 场景。

不是单元测试，而是**对抗性构造示范**：演示如何只通过控制 MaaS Monitor API 返回的 rows 来探测 `analyze_cluster` 的行为，并给出预期输出。

## 构造目标

1. 让首查窗口 `[reported_at, reported_at + 1min)` 的 system_ttft 落在 `(SLA, SLA * severe_ratio)` 的中间区，强制走 `second_window_history` 分支。
2. 在事件期 `[10:00, 10:09]` 连续 10 分钟轻度超 TTFT SLA，命中 `mild_consecutive_windows` 触发持续异常。
3. TPOT 始终 ≤ SLA，使事件 scope 落到 `ttft_only` 而不是 `both`。
4. 用户 B 是老用户，事件期 RPM 80→200、prompt 500→1500 突增。
5. 用户 C 没有任何历史记录，事件期首次出现（rpm=50, prompt=2000），触发 `event_variant = new_user_join_event`。

## 预期算法输出

与代码 100% 对得上即说明算法实现与设计一致：

| 字段 | 期望值 |
|---|---|
| `status` | `"anomaly"` |
| `decision_source` | `"second_window_history"` |
| `query_count` | `2` |
| `event_reports[0].rootcause_scope` | `"ttft_only"` |
| `event_reports[0].duration_hours` | `10` |
| `event_reports[0].event_variant` | `"new_user_join_event"` |
| `event_reports[0].new_join_users` | `["user-C"]` |
| `event_reports[0].driver_signal` | `"input_shift_dominant"` 或 `"rpm_input_mixed"` |
| `event_reports[0].culprits` | `[user-B, user-C]`（B 在前，C 在后） |
| `culprits[0].driver_signal` | `"rpm_rise_dominant"`（B 流量主导） |
| `culprits[1].driver_signal` | `"input_shift_dominant"`（C 输入主导） |
| `culprits[1].is_new_user_join` | `true` |

## 运行

```powershell
# 重新生成 CSV + scenario.json 并自验证
uv run python tests/scenarios/ttft_only_new_user/build.py

# 直接读取已有 CSV 跑算法
uv run python tests/run_scenario.py tests/scenarios/ttft_only_new_user/
```
