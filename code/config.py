from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


# 项目根目录，其他默认路径都从这里派生。
APP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = APP_DIR.parent

# 原始 CSV 默认输入目录。
RAW_DATA_DIR = PROJECT_ROOT / "data2"

# 结果文件默认输出目录。
RESULT_DIR = PROJECT_ROOT / "result"

# Web 上传文件的临时保存目录。
UPLOAD_DIR = PROJECT_ROOT / "_uploads"

# 预处理后的 SQLite 明细库默认路径。
PROCESSED_SQLITE_PATH = RESULT_DIR / "new_data_processed.sqlite"

# 聚合后的 SQLite 结果库默认路径。
AGGREGATED_SQLITE_PATH = RESULT_DIR / "new_data_aggregated.sqlite"

# 聚合脚本默认时间粒度。
DEFAULT_AGGREGATION_GRANULARITY = "1h"

# 标识“全部服务”的名称。
POOL_ALL_MARKERS = frozenset({"ALL", "全部", "_ALL_"})

# Web 下拉框中全部服务的展示名。
POOL_ALL_SERVICE_LABEL = "全部服务"

# Web 下拉框中无服务名时的展示名。
POOL_UNNAMED_SERVICE_LABEL = "未命名服务"

# Flask secret key 的环境变量名。
FLASK_SECRET_ENV = "FLASK_SECRET_KEY"

# 本地开发默认 Flask secret key。
DEFAULT_FLASK_SECRET_KEY = "local-dev-secret"

# Web 默认监听地址。
WEB_HOST = "127.0.0.1"

# Web 默认监听端口。
WEB_PORT = 5000

# 本地运行时是否开启 Flask debug。
WEB_DEBUG = True

# Web 默认灵敏度档位。
DEFAULT_SENSITIVITY = "balanced"

# Web 默认最多保留的事件数选项。
DEFAULT_MAX_EVENTS_OPTION = "5"

# Web 表单中表示不裁剪事件数的选项。
MAX_EVENTS_ALL_OPTION = "all"

# Web 页面可选的事件保留数量。
MAX_EVENTS_OPTIONS = ("2", "5", "10", MAX_EVENTS_ALL_OPTION)

# Web 页面灵敏度展示顺序。
SENSITIVITY_OPTIONS = ("sensitive", "balanced", "relaxed")

# 灵敏度档位对应的重度异常阈值倍率。
LATENCY_SENSITIVITY_RATIOS = {
    "sensitive": 4,
    "balanced": 7,
    "relaxed": 10,
}

# 灵敏度档位在页面上的展示名。
LATENCY_SENSITIVITY_LABELS = {
    "sensitive": "灵敏",
    "balanced": "均衡",
    "relaxed": "宽松",
}

# Plotly 生成 HTML 时是否内联 JS。
PLOTLY_INCLUDE_JS = "inline"

# Plotly 图表主题。
CHART_TEMPLATE = "plotly_white"

# 页面图表宽度。
CHART_WIDTH = 980

# 页面图表边距。
CHART_MARGIN = dict(l=52, r=32, t=60, b=44)

# 系统总览图高度。
SYSTEM_CHART_HEIGHT = 680

# 单用户详情图高度。
USER_CHART_HEIGHT = 860

# 耗时饼图高度。
TIMING_PIE_HEIGHT = 320

# 耗时分析最多展示的主要阶段数量，剩余阶段合并为“其他”。
TIMING_PIE_TOP_N = 6

# 判断除数接近 0 时使用的小量。
EPSILON = 1e-9

# 主导信号需要超过对方多少倍才认为明显占优。
DOMINANCE_RATIO = 1.2

# 输入/输出 token 变化判定的单项最小占比。
LENGTH_SIGNAL_MIN_RATIO = 0.15

# 输入/输出 token 联合变化判定的最小占比。
LENGTH_SIGNAL_JOINT_MIN_RATIO = 0.30

# 不同异常范围下的根因评分权重。
SCORE_WEIGHTS_BY_SCOPE = {
    "ttft_only": (0.35, 0.15, 0.40, 0.10),
    "tpot_only": (0.10, 0.25, 0.15, 0.50),
    "both": (0.225, 0.20, 0.275, 0.30),
}


@dataclass(frozen=True)
class LatencyDetectorConfig:
    # TTFT SLA 阈值，单位毫秒。
    ttft_sla: float = 15000.0

    # TPOT SLA 阈值，单位毫秒。
    tpot_sla: float = 50.0

    # 单点重度异常阈值倍率。
    severe_ratio: float = 7

    # 轻度异常需要连续命中的窗口数。
    mild_consecutive_windows: int = 10

    # 合并相邻事件时允许的空白窗口数。
    event_merge_gap: int = 0

    # 最多保留的事件数，0 表示不裁剪。
    max_events: int = 5

    # 基线滚动窗口点数。
    baseline_window_points: int = 24

    # 计算基线所需的最少历史点数。
    min_baseline_points: int = 6

    # 每个事件最多展示的根因用户数。
    culprit_top_k: int = 3

    # 根因用户累计贡献达到该比例后停止追加。
    culprit_cum_ratio: float = 0.8

    # 单个根因用户的最小贡献比例。
    culprit_min_ratio: float = 0.05


# 默认检测配置实例，供页面兜底展示使用。
DEFAULT_LATENCY_CONFIG = LatencyDetectorConfig()


# 演示数据随机种子基准值。
FAKE_DATA_SEED_BASE = 20260402
