# 当前异常检测逻辑卡片

本卡片是当前 [code/latency_detector.py](code/latency_detector.py) 的简版权威说明，便于快速回答“代码到底怎么判异常、怎么找主因”。

## 核心原则

```text
系统事件：由 TTFT / TPOT 是否超过 SLA 触发
主因定位：由 RPM / TPM / 输入长度 / 输出长度相对基线的正向变化解释
输出用户：只输出事件窗口内得分最高的 culprit 及其峰值点
```

当前逻辑不是流量总量检测器。系统 RPM/TPM 不直接触发事件，只参与根因解释。

## 1. 输入

必需字段：

```text
domain_id
collect_time_std
rpm
tpm
ttft_avg
tpot_avg
prompt_tokens
completion_tokens
```

预处理：

- 清理空 `domain_id`
- 数值列非法值填 0
- 解析 `collect_time_std`
- 按 `domain_id + collect_time_std` 去重折叠
- 构建连续时间轴，缺失窗口填 0

重复折叠规则：

- `rpm / tpm` 求和
- `ttft_avg / tpot_avg` 按 `rpm` 加权平均，忽略 0
- `prompt_tokens / completion_tokens` 按 `rpm` 加权平均

## 2. 系统序列

```text
system_rpm        = sum_user(rpm)
system_tpm        = sum_user(tpm)
system_ttft       = weighted_avg_user(ttft_avg, rpm), ignore zero latency
system_tpot       = weighted_avg_user(tpot_avg, rpm), ignore zero latency
system_prompt     = weighted_avg_user(prompt_tokens, rpm)
system_completion = weighted_avg_user(completion_tokens, rpm)
```

只有 `system_ttft` 和 `system_tpot` 参与事件触发。

## 3. 事件触发

默认参数：

```text
ttft_sla = 15000 ms
tpot_sla = 50 ms
severe_ratio = 7
mild_consecutive_windows = 10
event_merge_gap = 0
max_events = 5
```

单指标异常：

```text
heavy = metric >= sla * severe_ratio
mild = metric > sla
anom = heavy OR mark_runs(mild, mild_consecutive_windows)
```

系统异常：

```text
sys_anom_ttft = anom(system_ttft)
sys_anom_tpot = anom(system_tpot)
sys_anom = sys_anom_ttft OR sys_anom_tpot
```

事件窗口：

```text
events = mask_to_events(sys_anom, event_merge_gap)
events = cap_events_by_max_latency_ratio(events, max_events)
sys_event_mask = union(events)
```

`cap_events_by_max_latency_ratio` 使用窗口内 `max(TTFT/SLA, TPOT/SLA)` 作为排序严重度。

## 4. 事件范围

每个事件窗口 `[a, b]` 会被标成：

```text
ttft_only: 窗口内有 TTFT 异常，无 TPOT 异常
tpot_only: 窗口内有 TPOT 异常，无 TTFT 异常
both:      窗口内 TTFT 和 TPOT 都异常
```

该范围决定根因评分权重和诱因标签解释方式。

## 5. 基线

根因指标使用 past-only rolling mean：

```text
baseline = rolling_mean(metric.shift(1), window=24, min_periods=6)
NaN -> 0
```

用户级基线：

```text
rpm
tpm
prompt_tokens
completion_tokens
```

系统级基线：

```text
system_rpm
system_tpm
system_prompt
system_completion
```

## 6. 用户根因评分

事件窗口内，每个用户计算四类正向变化：

```text
rpm_excess        = max(rpm - baseline_rpm, 0)
tpm_excess        = max(tpm - baseline_tpm, 0)
prompt_delta      = max(prompt_tokens - baseline_prompt, 0)
completion_delta  = max(completion_tokens - baseline_completion, 0)
```

每类变化先按时间求和，再按用户归一化为贡献占比：

```text
rpm_excess_ratio
tpm_excess_ratio
prompt_delta_ratio
completion_delta_ratio
```

权重：

| scope | RPM | TPM | Prompt | Completion |
| --- | ---: | ---: | ---: | ---: |
| `ttft_only` | 0.35 | 0.15 | 0.40 | 0.10 |
| `tpot_only` | 0.10 | 0.25 | 0.15 | 0.50 |
| `both` | 0.225 | 0.20 | 0.275 | 0.30 |

得分：

```text
score =
  w_rpm * rpm_excess_ratio
+ w_tpm * tpm_excess_ratio
+ w_prompt * prompt_delta_ratio
+ w_completion * completion_delta_ratio
```

## 7. Culprit 选择

按 `score` 降序选择：

```text
最多 culprit_top_k = 3 个
累计 score_ratio >= 0.8 时停止
除第一个外，score_ratio < 0.05 时停止
score <= 0 时停止
```

每个 culprit 的 `peak_hour` 不是简单取 RPM 峰值，而是取事件窗口内综合局部得分最高的小时：

```text
flags[user, peak_hour] = True
```

因此当前 `flags` 表示“主因用户的代表性峰值点”，不是旧版完整用户异常 episode。

## 8. 诱因标签

事件级先算系统 shift score：

```text
positive_shift_score =
  positive_delta_sum / (baseline_abs_sum + positive_delta_sum + EPSILON)
```

比较方式：

```text
ttft_only: RPM vs 输入长度
tpot_only: TPM vs 输出长度
both:      流量家族 vs 长度家族
```

一侧至少是另一侧 `1.2` 倍时输出 dominant，否则输出 mixed；两侧都为 0 时输出 `unclear`。

用户级 `driver_signal` 使用该用户自己的四类贡献占比，同样按上述规则判断。

## 9. 新用户加入

```text
first_active_hour = first(rpm + tpm > 0)
```

如果 culprit 的 `first_active_hour` 位于事件窗口内：

```text
culprit.is_new_user_join = true
event.is_new_user_join_event = true
event.event_variant = new_user_join_event
```

否则事件为：

```text
traditional_latency_event
```

## 10. 当前未使用的旧逻辑

当前代码没有使用：

- RPM 总量季节性 ratio 门
- robust z / MAD
- 增长率 burst
- share z
- abs z
- 用户 growth burst
- episode 峰值回填
- 事件外用户异常检测

如果需要恢复这些能力，需要在 `latency_detector.py` 中重新实现，而不是只修改文档。

## 11. 与过载溯源工作流图的对照

本节把"异常过载状态识别与异常用户精筛"工作流图中的每个方框映射到当前代码符号，便于核对算法对齐性。

### 11.1 Step 1：异常事件识别

| 图中环节 | 当前实现 |
| --- | --- |
| 输入：短期用户时间数据 TTFT/TPOT/RPM，聚合粒度 分钟级 | `validate_latency_input` + `_collapse_duplicate_rows` 校验和折叠；在线模式由 `code/maas_monitor_cli.py:rows_to_latency_frame` 把 MaaS API 行转成同结构 DataFrame |
| 构建系统级序列：生成系统级 TTFT/TPOT，按 RPM 加权 | [code/latency_detector.py](code/latency_detector.py) `_build_system_series` 用 `_weighted_average_ignore_zero_1d` 对 `ttft_avg / tpot_avg` 做 RPM 加权 |
| 重度异常判定：`Metric > SLA × ratio`（三档阈值，均衡告警数与灵敏度） | `_detect_system_events` 中 `ttft_heavy = system_ttft >= ttft_sla * severe_ratio`、`tpot_heavy = system_tpot >= tpot_sla * severe_ratio`；三档阈值由 [code/config.py](code/config.py) `LATENCY_SENSITIVITY_RATIOS = {sensitive:4, balanced:7, relaxed:10}` 提供 |
| 标记重度异常：单个窗口超阈值，立即记为异常 | `ttft_heavy / tpot_heavy` 单点掩码直接进入 `sys_anom_ttft / sys_anom_tpot` |
| 轻度且持续异常：`Metric > SLA` 且连续窗口 ≥ N | `ttft_mild = system_ttft > ttft_sla`；`_mark_runs(ttft_mild, mild_consecutive_windows)`，默认 `mild_consecutive_windows = 10` |
| 标记持续异常 | `sys_anom_ttft = ttft_heavy OR mark_runs(ttft_mild)`（TPOT 同理） |
| 并集触发 + 事件合并 | `sys_anom = sys_anom_ttft OR sys_anom_tpot`；`_mask_to_events(sys_anom, event_merge_gap)` 把连续异常分钟收敛成 `(start, end)` 事件窗口 |
| 输出：异常时刻结果（掩码、TTFT/TPOT 分量、系统级命中） | 返回字段 `sys_anom / sys_anom_ttft / sys_anom_tpot / sys_event_mask` |
| 输出：异常时刻列表（start/end/duration/ttft_only/tpot_only/both/系统峰值时间与大小） | `event_reports[*]` 的 `start_hour / end_hour / duration_hours / rootcause_scope / system_peak_hour_ttft / system_peak_ttft / system_peak_hour_tpot / system_peak_tpot` |

工程化扩展（图未画但已实现）：`max_events` 用 `max(TTFT/SLA, TPOT/SLA)` 排序后裁剪事件数，避免长时间段产生过多事件。

### 11.2 Step 2：根因用户定位

| 图中环节 | 当前实现 |
| --- | --- |
| 输入：异常事件窗口数据 (start/end，事件范围 ttft/tpot/双异常) | 上一步产出的 `event_reports` 直接驱动；`_scope_for_window(sys_anom_ttft, sys_anom_tpot, a, b)` 把每个窗口标成 `ttft_only / tpot_only / both` |
| 输入：用户历史数据 (RPM/TPM/输入长度/输出长度，分钟/小时/半小时聚合) | `_build_metric_matrices` 构建用户×时间的四维矩阵；在线模式受 MaaS API `granularity=minute` 约束，仅使用分钟粒度 |
| 构建历史滚动基线：用户当前时刻 rpm / tpm / 输入 / 输出 四维预测基线 | 两套基线实现：① 离线连续序列模式 `_rolling_baselines` 用 `rolling_mean(shift(1), window=24, min_periods=6)`；② 在线 MaaS 模式 `_historical_baselines_by_offset`，用过去 14 天**同一时刻 ±10min 同分钟偏移**的均值（在 `detect_latency_anomalies_with_history` 中调用）。后者更贴近图中"用户当前时刻预测基线"的语义 |
| 计算异常误差变化量：`max(metric - baseline, 0)` | `np.clip(rpm_matrix - baseline_rpm, 0.0, None)`，对 `rpm / tpm / prompt_tokens / completion_tokens` 各算一份 |
| 归一化：用户间按异常贡献占比归一化得 `rpm_ratio` 等 | `_safe_ratio(rpm_excess_sum)` 把窗口内各用户的正向变化和除以全局和 |
| 当前事件类型 → 灵活调整四维得分权重 | `SCORE_WEIGHTS_BY_SCOPE` 按 scope 分发权重（对应图中三种事件类型）|
| ttft_only：权重侧重 RPM + 输入 | `(w_rpm, w_tpm, w_prompt, w_completion) = (0.35, 0.15, 0.40, 0.10)` |
| tpot_only：权重侧重 TPM + 输出 | `(0.10, 0.25, 0.15, 0.50)` |
| 双异常：均衡输入输出 | `(0.225, 0.20, 0.275, 0.30)` |
| 计算得分并排序：`bScore = w_rpm·rpm_ratio + w_tpm·tpm_ratio + w_输入·input_ratio + w_输出·output_ratio` | `scores = w_rpm*rpm_ratio + w_tpm*tpm_ratio + w_prompt*prompt_ratio + w_completion*completion_ratio`；`np.argsort(scores)[::-1]` 降序 |
| 输出：根因用户列表，按 bScore 排序，结合占比规则截断得 Top K | 截断由三组阈值共同决定：`culprit_top_k = 3`（硬上限）/ `culprit_cum_ratio = 0.8`（累计贡献占比触发停止）/ `culprit_min_ratio = 0.05`（除头名外的单用户最小占比） |
| 输出：用户诊断标注（请求异常 / 输入输出模式异常 / 用户侧诱因解释） | 每个 culprit 输出 `driver_signal`（如 `rpm_rise_dominant / input_shift_dominant / rpm_input_mixed`，由 `_culprit_driver_signal` 给出）和 `length_signal`（七分类，见 `_length_signal`），事件级再额外输出 `event_reports[*].driver_signal / traffic_driver_ratio / length_driver_ratio` |

工程化扩展（图未画但已实现）：
- **新用户加入事件**：若 culprit 的 `first_active_hour` 落在事件窗口内，事件被标为 `event_variant = new_user_join_event`，可用于区分"老用户突发流量"与"新用户冷启动"。
- **峰值小时定位**：每个 culprit 输出 `peak_hour` —— 不是其 RPM 峰值，而是窗口内综合局部得分最高的时刻，配合峰值 RPM/TPM/TTFT/TPOT 给出更可解释的代表性点。

### 11.3 数据粒度差异说明

| 工作流图 | 当前实现 |
| --- | --- |
| 用户历史数据聚合粒度：分钟 / 小时 / 半小时 | MaaS Monitor API 目前只支持 `granularity=minute`（见 [MAAS_MONITOR_API.md](MAAS_MONITOR_API.md) 3.2 节），因此在线模式只走分钟粒度；离线 CSV/SQLite 模式仍可支持 1h 等粗粒度（由 `_infer_time_freq` 自动推断）|

如果未来 MaaS API 放开半小时/小时粒度，只需把 `detect_latency_anomalies_with_history` 中 `freq = pd.Timedelta(minutes=1)` 改成动态值并调整时间窗口长度即可，下游评分逻辑无需改动。

### 11.4 对齐结论

工作流图描述的"系统级事件识别 + 用户级 bScore 根因评分"双步管线，与 [code/latency_detector.py](code/latency_detector.py) 的实现**逐环节对应**。当前代码在图的基础上加入了灵敏度三档阈值具体取值、事件数上限、新用户加入识别、峰值小时定位、七分类长度诱因标签等工程化能力，但未引入图外的额外触发路径，整体不偏离原始算法语义。
