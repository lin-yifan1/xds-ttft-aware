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

