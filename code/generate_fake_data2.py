from __future__ import annotations

import argparse
from collections.abc import Iterator
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
import math
from pathlib import Path
import uuid

import numpy as np

from config import FAKE_DATA_SEED_BASE, RAW_DATA_DIR


CSV_HEADER = [
    "service_name",
    "domain_id",
    "card_model",
    "rpm",
    "tpm",
    "ttft_avg",
    "tpot_avg",
    "req_count_4xx",
    "req_count_5xx",
    "req_error_rate",
    "total_tokens",
    "prompt_tokens",
    "completion_tokens",
    "latency_avg",
    "req_count",
    "req_count_2xx",
    "req_count_error",
    "prompt_tokens_avg",
    "prompt_tokens_p50",
    "prompt_tokens_p80",
    "prompt_tokens_p90",
    "prompt_tokens_p99",
    "prompt_tokens_max",
    "completion_tokens_avg",
    "completion_tokens_p50",
    "completion_tokens_p80",
    "completion_tokens_p90",
    "completion_tokens_p99",
    "completion_tokens_max",
    "ttft_p50",
    "ttft_p80",
    "ttft_p90",
    "ttft_p99",
    "ttft_max",
    "tpot_p50",
    "tpot_p80",
    "tpot_p90",
    "tpot_p99",
    "tpot_max",
    "collect_time_std",
    "prompt_tpm",
    "completion_tpm",
    "infer_service_id",
    "pool_id",
    "region",
    "instance_count",
    "infer_engine",
    "prompt_tps",
    "completion_tps",
    "total_tps",
    "service_type",
    "stream",
    "sub_models",
]

DEFAULT_TOTAL_ROWS = 15_895_394
DEFAULT_FILE_COUNT = 106
DEFAULT_START = datetime(2026, 4, 2, 0, 0, 0)
DEFAULT_DAYS = 8
DEFAULT_CUSTOMER_COUNT = 3_970
DEFAULT_MINUTE_MEAN = 1_380
DEFAULT_MINUTE_STD = 283
DEFAULT_MINUTE_MIN = 810
DEFAULT_MINUTE_MAX = 2_320
DEFAULT_CHUNK_ROWS = 50_000

DAY_WEIGHTS = np.array(
    [
        0.1235,
        0.1242,
        0.1141,
        0.1089,
        0.1126,
        0.1323,
        0.1385,
        0.1459,
    ],
    dtype=float,
)
HOUR_WEIGHTS = np.array(
    [
        0.0390,
        0.0367,
        0.0344,
        0.0318,
        0.0299,
        0.0293,
        0.0300,
        0.0330,
        0.0394,
        0.0456,
        0.0484,
        0.0485,
        0.0436,
        0.0443,
        0.0481,
        0.0502,
        0.0501,
        0.0492,
        0.0459,
        0.0452,
        0.0457,
        0.0456,
        0.0443,
        0.0419,
    ],
    dtype=float,
)

UUID_NAMESPACE = uuid.UUID("45f1ef34-a73c-4c8d-a71f-bc2fa8b28970")

DEFAULT_CARD_MODEL_WEIGHTS = (
    ("D910B", 0.52),
    ("D910C", 0.38),
    ("D910C,D910B", 0.10),
)
DEFAULT_REGION_WEIGHTS = (
    ("cn-north-9", 0.42),
    ("cn-north-12", 0.35),
    ("cn-southwest-2", 0.15),
    ("cn-east-4", 0.08),
)
REGION_POOL_IDS = {
    "cn-north-9": (
        "os-cn9-001-infer2-maas-ds",
        "os-cn9-002-infer2-maas-glm",
        "os-cn9-003-infer2-maas-qwen",
        "os-cn9-004-infer2-maas-embed",
        "pool-infer-v2-maas-superpod-cn9-a",
        "pool-infer-v2-maas-superpod-cn9-b",
    ),
    "cn-north-12": (
        "os-cn12-001-infer2-maas-ds",
        "os-cn12-002-infer2-maas-r1",
        "os-cn12-003-infer2-maas-v3",
        "os-cn12-004-infer2-maas-kimi",
        "pool-infer-v2-maas-superpod-cn12-a",
        "pool-infer-v2-maas-superpod-cn12-b",
    ),
    "cn-southwest-2": (
        "os-csw2-001-infer2-maas-qwen",
        "os-csw2-002-infer2-maas-vl",
        "os-csw2-003-infer2-maas-coder",
        "pool-infer-v2-maas-superpod-csw2-a",
        "pool-infer-v2-maas-superpod-csw2-b",
    ),
    "cn-east-4": (
        "os-cne4-001-infer2-maas-mix",
        "os-cne4-002-infer2-maas-ernie",
        "os-cne4-003-infer2-maas-doubao",
        "pool-infer-v2-maas-superpod-cne4-a",
        "pool-infer-v2-maas-superpod-cne4-b",
    ),
}


@dataclass(frozen=True)
class ChoiceTable:
    values: tuple[object, ...]
    cumulative: np.ndarray

    @classmethod
    def from_pairs(cls, pairs: tuple[tuple[object, float], ...]) -> "ChoiceTable":
        if not pairs:
            raise ValueError("ChoiceTable requires at least one choice.")
        values = tuple(value for value, _ in pairs)
        weights = normalize_weights([weight for _, weight in pairs])
        return cls(values=values, cumulative=np.cumsum(weights))


@dataclass(frozen=True)
class ServiceProfile:
    name: str
    row_weight: float
    domain_count: int
    infer_service_count: int
    service_type_weights: tuple[tuple[int, float], ...]
    stream_true_ratio: float
    sub_model_weights: tuple[tuple[str, float], ...]
    infer_engine_weights: tuple[tuple[str, float], ...]
    card_model_weights: tuple[tuple[str, float], ...] = DEFAULT_CARD_MODEL_WEIGHTS
    region_weights: tuple[tuple[str, float], ...] = DEFAULT_REGION_WEIGHTS


@dataclass(frozen=True)
class RuntimeProfile:
    profile: ServiceProfile
    domain_table: ChoiceTable
    infer_service_table: ChoiceTable
    service_type_table: ChoiceTable
    sub_model_table: ChoiceTable
    infer_engine_table: ChoiceTable
    card_model_table: ChoiceTable
    region_table: ChoiceTable
    is_embedding_like: bool


class PartitionedCsvWriter:
    def __init__(self, output_dir: Path, file_counts: list[int]) -> None:
        self.output_dir = output_dir
        self.file_counts = file_counts
        self.digits = max(3, len(str(len(file_counts))))
        self.file_index = 0
        self.rows_remaining = 0
        self.current_handle = None
        self.current_writer = None
        self.current_tmp_path: Path | None = None
        self.current_final_path: Path | None = None
        self.completed_paths: list[Path] = []
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def __enter__(self) -> "PartitionedCsvWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close(finalize=exc_type is None)

    def write_columns(self, columns: dict[str, np.ndarray]) -> None:
        total = len(columns[CSV_HEADER[0]])
        for name in CSV_HEADER[1:]:
            if len(columns[name]) != total:
                raise ValueError(f"Column length mismatch for {name}.")

        start = 0
        while start < total:
            if self.current_writer is None or self.rows_remaining == 0:
                self._advance_to_next_writable_file()
            if self.current_writer is None:
                raise RuntimeError("No output files remain for generated rows.")

            take = min(total - start, self.rows_remaining)
            end = start + take
            row_columns = [columns[name][start:end] for name in CSV_HEADER]
            self.current_writer.writerows(zip(*row_columns))
            self.rows_remaining -= take
            start = end

    def close(self, finalize: bool = True) -> None:
        self._close_current_file(finalize=finalize)
        if finalize:
            while self.file_index < len(self.file_counts):
                self._open_next_file()
                self._close_current_file(finalize=True)

    def _advance_to_next_writable_file(self) -> None:
        self._close_current_file(finalize=True)
        while self.file_index < len(self.file_counts):
            self._open_next_file()
            if self.rows_remaining > 0:
                return
            self._close_current_file(finalize=True)

    def _open_next_file(self) -> None:
        self.file_index += 1
        final_path = self.output_dir / f"output_part_{self.file_index:0{self.digits}d}.csv"
        tmp_path = final_path.with_name(f"{final_path.name}.tmp")
        handle = tmp_path.open("w", encoding="utf-8", newline="", buffering=1024 * 1024)
        writer = csv.writer(handle, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(CSV_HEADER)
        self.current_handle = handle
        self.current_writer = writer
        self.current_tmp_path = tmp_path
        self.current_final_path = final_path
        self.rows_remaining = self.file_counts[self.file_index - 1]

    def _close_current_file(self, finalize: bool) -> None:
        if self.current_handle is None:
            return
        self.current_handle.close()
        tmp_path = self.current_tmp_path
        final_path = self.current_final_path
        self.current_handle = None
        self.current_writer = None
        self.current_tmp_path = None
        self.current_final_path = None
        if finalize and tmp_path is not None and final_path is not None:
            tmp_path.replace(final_path)
            self.completed_paths.append(final_path)


def normalize_weights(weights: list[float] | tuple[float, ...] | np.ndarray) -> np.ndarray:
    arr = np.asarray(weights, dtype=float)
    total = float(arr.sum())
    if total <= 0:
        raise ValueError("Weights must sum to a positive value.")
    return arr / total


def largest_remainder_counts(weights: list[float] | tuple[float, ...] | np.ndarray, total: int) -> np.ndarray:
    if total < 0:
        raise ValueError("total must be >= 0")
    weights_arr = normalize_weights(weights)
    shares = weights_arr * total
    counts = np.floor(shares).astype(np.int64)
    remaining = int(total - counts.sum())
    if remaining > 0:
        order = np.argsort(shares - counts)[::-1]
        counts[order[:remaining]] += 1
    return counts


def bounded_largest_remainder(raw_weights: np.ndarray, total: int, lower: int, upper: int) -> np.ndarray:
    n = len(raw_weights)
    if n == 0:
        return np.array([], dtype=np.int64)
    if total < 0:
        raise ValueError("total must be >= 0")
    lower = max(0, lower)
    if total < n * lower:
        lower = total // n
    if upper < lower:
        upper = lower
    if total > n * upper:
        upper = math.ceil(total / n)

    base = np.full(n, lower, dtype=np.int64)
    remaining = int(total - base.sum())
    if remaining <= 0:
        return base

    capacities = np.full(n, upper - lower, dtype=np.int64)
    weights = np.asarray(raw_weights, dtype=float)
    weights = np.where(weights > 0, weights, 1.0)
    shares = normalize_weights(weights) * remaining
    additions = np.minimum(np.floor(shares).astype(np.int64), capacities)
    counts = base + additions
    remainder = int(total - counts.sum())
    fractions = shares - np.floor(shares)

    while remainder > 0:
        eligible = np.flatnonzero(counts < upper)
        if len(eligible) == 0:
            raise RuntimeError("Unable to allocate bounded counts.")
        order = eligible[np.argsort(fractions[eligible])[::-1]]
        take = min(remainder, len(order))
        counts[order[:take]] += 1
        remainder -= take

    return counts


def power_weights(n: int, alpha: float = 1.12) -> np.ndarray:
    ranks = np.arange(1, n + 1, dtype=float)
    return normalize_weights(1.0 / np.power(ranks, alpha))


def build_service_profiles() -> list[ServiceProfile]:
    major_profiles = [
        ServiceProfile(
            "DeepSeek-V3.2",
            0.702,
            2_648,
            20,
            ((2, 0.94), (4, 0.06)),
            0.37,
            (("deepseek-v3.2", 1.0),),
            (("fabric", 1.0),),
            region_weights=(("", 1.0),),
        ),
        ServiceProfile(
            "GLM-5",
            0.080,
            756,
            4,
            ((2, 0.98), (4, 0.02)),
            0.66,
            (("glm-5", 1.0),),
            (("fabric", 1.0),),
            region_weights=(("", 1.0),),
        ),
        ServiceProfile(
            "DeepSeek-V3",
            0.073,
            421,
            13,
            ((2, 0.88), (4, 0.07), (1, 0.05)),
            0.36,
            (("DeepSeek-V3", 1.0),),
            (("fabric", 0.76), ("modelarts", 0.24)),
            card_model_weights=(("D910C", 1.0),),
            region_weights=(("cn-north-12", 1.0),),
        ),
        ServiceProfile(
            "DeepSeek-R1-0528",
            0.039,
            209,
            4,
            ((2, 0.98), (4, 0.02)),
            0.42,
            (("deepseek-r1-250528", 1.0),),
            (("fabric", 1.0),),
            region_weights=(("", 1.0),),
        ),
        ServiceProfile(
            "Qwen3-32B",
            0.016,
            105,
            4,
            ((2, 1.0),),
            0.28,
            (("qwen3-32b", 1.0),),
            (("modelarts_infer_v2", 1.0),),
            card_model_weights=(("D910B", 1.0),),
            region_weights=(("cn-north-9", 0.65), ("cn-southwest-2", 0.35)),
        ),
        ServiceProfile(
            "DeepSeek-V3.1",
            0.013,
            260,
            6,
            ((2, 0.96), (4, 0.04)),
            0.32,
            (("deepseek-v3.1-terminus", 0.86), ("deepseek-v3.1", 0.14)),
            (("fabric", 0.82), ("modelarts", 0.18)),
            card_model_weights=(("D910C", 1.0),),
            region_weights=(("cn-north-12", 1.0),),
        ),
        ServiceProfile(
            "Kimi-K2",
            0.013,
            274,
            4,
            ((2, 1.0),),
            0.54,
            (("Kimi-K2", 1.0),),
            (("fabric", 1.0),),
            region_weights=(("", 1.0),),
        ),
        ServiceProfile(
            "DeepSeek-R1",
            0.009,
            109,
            8,
            ((2, 0.62), (1, 0.38)),
            0.21,
            (("DeepSeek-R1", 1.0),),
            (("modelarts", 0.85), ("fabric", 0.15)),
            card_model_weights=(("D910C", 1.0),),
            region_weights=(("cn-north-12", 1.0),),
        ),
        ServiceProfile(
            "Qwen3-235B-A22B",
            0.007,
            178,
            3,
            ((2, 1.0),),
            0.47,
            (("qwen3-235b-a22b", 1.0),),
            (("fabric", 1.0),),
            region_weights=(("", 1.0),),
        ),
        ServiceProfile(
            "BGE-M3",
            0.006,
            144,
            2,
            ((2, 1.0),),
            0.0,
            (("bge-m3", 1.0),),
            (("modelarts_infer_v2", 1.0),),
            card_model_weights=(("D910B", 1.0),),
            region_weights=(("cn-north-9", 1.0),),
        ),
    ]

    minor_names = [
        "Qwen3-14B",
        "Qwen3-8B",
        "Qwen3-4B",
        "Qwen2.5-72B",
        "Qwen2.5-32B",
        "Qwen2.5-14B",
        "Qwen2.5-7B",
        "GLM-4.5",
        "GLM-4.5-Air",
        "GLM-4-Plus",
        "GLM-4-Air",
        "Hunyuan-T1",
        "Hunyuan-Standard",
        "Baichuan4-Turbo",
        "Baichuan3-Turbo",
        "ERNIE-4.5",
        "ERNIE-Speed",
        "Yi-Large",
        "Yi-Lightning",
        "MiniMax-Text-01",
        "MiniMax-M1",
        "Step-2",
        "Step-1",
        "InternLM3-8B",
        "InternLM2.5-20B",
        "Llama-3.3-70B",
        "Llama-3.1-8B",
        "Mistral-Large",
        "Mixtral-8x22B",
        "Baichuan-Embedding",
        "BGE-Large-ZH",
        "BGE-Reranker-V2",
        "text-embedding-v3",
        "multimodal-embedding",
        "Qwen-VL-Max",
        "Qwen-VL-Plus",
        "GLM-4V-Plus",
        "DeepSeek-Coder-V2",
        "CodeGeeX4",
        "Qwen2.5-Coder-32B",
        "Doubao-Pro-32k",
        "Doubao-Lite-32k",
        "Moonshot-v1-8k",
        "Moonshot-v1-32k",
        "Claude-Compatible",
        "GPT-Compatible",
        "Rerank-Gateway",
    ]
    remainder = 1.0 - sum(profile.row_weight for profile in major_profiles)
    raw_minor_weights = np.linspace(len(minor_names), 1, len(minor_names), dtype=float)
    minor_weights = normalize_weights(raw_minor_weights) * remainder
    minor_profiles = [
        build_minor_profile(idx, name, float(weight))
        for idx, (name, weight) in enumerate(zip(minor_names, minor_weights), start=1)
    ]

    profiles = major_profiles + minor_profiles
    if len(profiles) != 57:
        raise AssertionError(f"Expected 57 service profiles, got {len(profiles)}.")
    return profiles


def build_minor_profile(idx: int, name: str, row_weight: float) -> ServiceProfile:
    lower_name = name.lower()
    is_embedding_like = any(token in lower_name for token in ("embedding", "bge", "rerank"))
    is_visual = "vl" in lower_name or "4v" in lower_name or "multimodal" in lower_name
    is_qwen = lower_name.startswith("qwen")

    if idx <= 8:
        domain_count = 46 - idx * 3
    elif idx <= 20:
        domain_count = 8 + (idx % 7)
    else:
        domain_count = 1 + (idx % 5)

    infer_count = 1 + (idx % 4)
    if is_embedding_like:
        stream_true_ratio = 0.0
        service_type_weights = ((2, 0.82), (4, 0.18))
        infer_engine_weights = (("modelarts_infer_v2", 1.0),)
        card_model_weights = (("D910B", 0.75), ("D910C", 0.25))
        region_weights = (("cn-north-9", 0.75), ("cn-east-4", 0.25))
    elif is_visual:
        stream_true_ratio = 0.18 + (idx % 3) * 0.08
        service_type_weights = ((2, 0.90), (4, 0.10))
        infer_engine_weights = (("modelarts_infer_v2", 0.88), ("fabric", 0.12))
        card_model_weights = (("D910B", 0.65), ("D910C", 0.25), ("D910C,D910B", 0.10))
        region_weights = (("cn-southwest-2", 0.55), ("cn-north-9", 0.30), ("cn-east-4", 0.15))
    else:
        stream_true_ratio = 0.24 + ((idx * 17) % 43) / 100
        service_type_weights = ((2, 0.91), (1, 0.05), (4, 0.04))
        if is_qwen:
            infer_engine_weights = (("modelarts_infer_v2", 0.68), ("fabric", 0.32))
            region_weights = (("cn-north-9", 0.48), ("cn-southwest-2", 0.32), ("cn-east-4", 0.20))
        else:
            infer_engine_weights = (("fabric", 0.70), ("modelarts_infer_v2", 0.30))
            region_weights = DEFAULT_REGION_WEIGHTS
        card_model_weights = DEFAULT_CARD_MODEL_WEIGHTS

    return ServiceProfile(
        name=name,
        row_weight=row_weight,
        domain_count=domain_count,
        infer_service_count=infer_count,
        service_type_weights=service_type_weights,
        stream_true_ratio=stream_true_ratio,
        sub_model_weights=((name.lower(), 1.0),),
        infer_engine_weights=infer_engine_weights,
        card_model_weights=card_model_weights,
        region_weights=region_weights,
    )


def build_runtime_profiles(
    profiles: list[ServiceProfile],
    rng: np.random.Generator,
    customer_count: int,
) -> list[RuntimeProfile]:
    customer_ids = np.arange(1, customer_count + 1, dtype=np.int64)
    runtime_profiles: list[RuntimeProfile] = []
    for profile in profiles:
        domain_count = min(max(1, profile.domain_count), customer_count)
        selected_customers = rng.choice(customer_ids, size=domain_count, replace=False)
        domain_labels = tuple(f"客户{int(customer_id)}" for customer_id in selected_customers)
        domain_pairs = tuple(zip(domain_labels, power_weights(domain_count, alpha=1.16)))

        infer_ids = tuple(
            str(uuid.uuid5(UUID_NAMESPACE, f"{profile.name}:{idx}"))
            for idx in range(1, profile.infer_service_count + 1)
        )
        infer_pairs = tuple(zip(infer_ids, power_weights(profile.infer_service_count, alpha=0.82)))

        is_embedding_like = any(
            token in profile.name.lower()
            for token in ("embedding", "bge", "rerank")
        )
        runtime_profiles.append(
            RuntimeProfile(
                profile=profile,
                domain_table=ChoiceTable.from_pairs(domain_pairs),
                infer_service_table=ChoiceTable.from_pairs(infer_pairs),
                service_type_table=ChoiceTable.from_pairs(profile.service_type_weights),
                sub_model_table=ChoiceTable.from_pairs(profile.sub_model_weights),
                infer_engine_table=ChoiceTable.from_pairs(profile.infer_engine_weights),
                card_model_table=ChoiceTable.from_pairs(profile.card_model_weights),
                region_table=ChoiceTable.from_pairs(profile.region_weights),
                is_embedding_like=is_embedding_like,
            )
        )
    return runtime_profiles


def build_minute_schedule(
    start: datetime,
    days: int,
    total_rows: int,
    rng: np.random.Generator,
) -> list[tuple[datetime, int]]:
    if days != len(DAY_WEIGHTS):
        day_weights = np.ones(days, dtype=float)
    else:
        day_weights = DAY_WEIGHTS
    day_weights = normalize_weights(day_weights)
    hour_weights = normalize_weights(HOUR_WEIGHTS)

    hour_slots: list[tuple[datetime, float]] = []
    for day_idx in range(days):
        for hour_idx in range(24):
            hour_start = start + timedelta(days=day_idx, hours=hour_idx)
            hour_slots.append((hour_start, float(day_weights[day_idx] * hour_weights[hour_idx])))

    hour_counts = largest_remainder_counts([weight for _, weight in hour_slots], total_rows)
    production_scale = total_rows >= DEFAULT_MINUTE_MIN * days * 24 * 60
    schedule: list[tuple[datetime, int]] = []

    for (hour_start, _), hour_count_np in zip(hour_slots, hour_counts):
        hour_count = int(hour_count_np)
        mean = hour_count / 60 if hour_count else 0.0
        if production_scale:
            lower = DEFAULT_MINUTE_MIN
            upper = DEFAULT_MINUTE_MAX
            std = DEFAULT_MINUTE_STD
        else:
            lower = 1 if hour_count >= 60 else 0
            upper = max(1, math.ceil(mean * 3)) if hour_count else 0
            std = max(1.0, mean * DEFAULT_MINUTE_STD / DEFAULT_MINUTE_MEAN)

        if hour_count == 0:
            minute_counts = np.zeros(60, dtype=np.int64)
        else:
            raw = rng.normal(loc=max(mean, 1.0), scale=std, size=60)
            raw = np.clip(raw, max(0.001, lower), max(1, upper))
            minute_counts = bounded_largest_remainder(raw, hour_count, lower, upper)

        for minute_idx, minute_count in enumerate(minute_counts):
            schedule.append((hour_start + timedelta(minutes=minute_idx), int(minute_count)))

    scheduled_rows = sum(count for _, count in schedule)
    if scheduled_rows != total_rows:
        raise AssertionError(f"Minute schedule mismatch: {scheduled_rows} != {total_rows}.")
    return schedule


def pick_many(table: ChoiceTable, rng: np.random.Generator, size: int) -> np.ndarray:
    if size <= 0:
        return np.empty(0, dtype=object)
    indices = np.searchsorted(table.cumulative, rng.random(size), side="right")
    np.minimum(indices, len(table.values) - 1, out=indices)
    return np.asarray(table.values, dtype=object)[indices]


def sample_lognormal_array(
    rng: np.random.Generator,
    size: int,
    median: float,
    sigma: float,
    min_value: float,
    max_value: float,
    zero_prob: float | np.ndarray = 0.0,
    tail_prob: float = 0.0,
    tail_low: float = 2.0,
    tail_high: float = 8.0,
) -> np.ndarray:
    values = rng.lognormal(mean=math.log(max(median, 1e-9)), sigma=sigma, size=size)
    if tail_prob:
        tail_mask = rng.random(size) < tail_prob
        if tail_mask.any():
            values[tail_mask] *= rng.uniform(tail_low, tail_high, int(tail_mask.sum()))
    np.clip(values, min_value, max_value, out=values)

    if np.isscalar(zero_prob):
        if float(zero_prob) > 0:
            values[rng.random(size) < float(zero_prob)] = 0.0
    else:
        zero_probs = np.asarray(zero_prob, dtype=float)
        values[rng.random(size) < zero_probs] = 0.0
    return values


def sample_percentiles_array(
    rng: np.random.Generator,
    center: np.ndarray,
    multipliers: tuple[tuple[float, float], ...],
    max_cap: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    size = len(center)
    if size == 0:
        return tuple(np.empty(0, dtype=float) for _ in range(5))  # type: ignore[return-value]

    factors = np.column_stack([rng.uniform(low, high, size) for low, high in multipliers])
    values = center[:, None] * factors
    values = np.maximum.accumulate(values, axis=1)
    np.minimum(values, max_cap, out=values)
    values = np.maximum.accumulate(values, axis=1)
    return tuple(values[:, i] for i in range(5))  # type: ignore[return-value]


def pick_pool_ids(rng: np.random.Generator, regions: np.ndarray) -> np.ndarray:
    pool_ids = np.full(len(regions), "", dtype=object)
    for region in np.unique(regions):
        pools = REGION_POOL_IDS.get(str(region))
        if not pools:
            continue
        mask = regions == region
        choices = rng.integers(0, len(pools), int(mask.sum()))
        pool_ids[mask] = np.asarray(pools, dtype=object)[choices]
    return pool_ids


def iter_schedule_chunks(
    minute_schedule: list[tuple[datetime, int]],
    chunk_rows: int,
) -> Iterator[np.ndarray]:
    if chunk_rows < 1:
        raise ValueError("--chunk-rows must be >= 1")

    labels: list[str] = []
    counts: list[int] = []
    chunk_size = 0
    for collect_time, row_count in minute_schedule:
        remaining = row_count
        label = collect_time.strftime("%Y-%m-%d %H:%M:%S")
        while remaining > 0:
            take = min(remaining, chunk_rows - chunk_size)
            labels.append(label)
            counts.append(take)
            chunk_size += take
            remaining -= take
            if chunk_size == chunk_rows:
                yield np.repeat(np.asarray(labels, dtype=object), np.asarray(counts, dtype=np.int64))
                labels = []
                counts = []
                chunk_size = 0

    if chunk_size:
        yield np.repeat(np.asarray(labels, dtype=object), np.asarray(counts, dtype=np.int64))


def generate_chunk_columns(
    rng: np.random.Generator,
    runtime_profiles: list[RuntimeProfile],
    service_weights: np.ndarray,
    collect_time_std: np.ndarray,
) -> dict[str, np.ndarray]:
    row_count = len(collect_time_std)
    service_indices = rng.choice(len(runtime_profiles), size=row_count, p=service_weights)

    service_name = np.empty(row_count, dtype=object)
    domain_id = np.empty(row_count, dtype=object)
    card_model = np.full(row_count, "", dtype=object)
    infer_service_id = np.empty(row_count, dtype=object)
    pool_id = np.full(row_count, "", dtype=object)
    region = np.full(row_count, "", dtype=object)
    infer_engine = np.empty(row_count, dtype=object)
    service_type = np.empty(row_count, dtype=object)
    sub_models = np.empty(row_count, dtype=object)
    stream_bool = np.zeros(row_count, dtype=bool)
    embedding_like = np.zeros(row_count, dtype=bool)

    for service_idx in np.unique(service_indices):
        indices = np.flatnonzero(service_indices == service_idx)
        profile = runtime_profiles[int(service_idx)]
        size = len(indices)

        service_name[indices] = profile.profile.name
        domain_id[indices] = pick_many(profile.domain_table, rng, size)
        infer_service_id[indices] = pick_many(profile.infer_service_table, rng, size)
        service_type[indices] = pick_many(profile.service_type_table, rng, size)
        sub_models[indices] = pick_many(profile.sub_model_table, rng, size)
        infer_values = pick_many(profile.infer_engine_table, rng, size)
        infer_engine[indices] = infer_values
        stream_bool[indices] = rng.random(size) < profile.profile.stream_true_ratio
        embedding_like[indices] = profile.is_embedding_like

        non_fabric_local = infer_values != "fabric"
        if non_fabric_local.any():
            non_fabric_indices = indices[non_fabric_local]
            non_fabric_size = len(non_fabric_indices)
            card_model[non_fabric_indices] = pick_many(profile.card_model_table, rng, non_fabric_size)
            region_values = pick_many(profile.region_table, rng, non_fabric_size)
            region[non_fabric_indices] = region_values
            pool_id[non_fabric_indices] = pick_pool_ids(rng, region_values)

    req_count = np.rint(
        sample_lognormal_array(
            rng,
            row_count,
            median=65.0,
            sigma=1.16,
            min_value=1.0,
            max_value=25_180.0,
            tail_prob=0.004,
            tail_low=2.0,
            tail_high=18.0,
        )
    ).astype(np.int64)
    np.maximum(req_count, 1, out=req_count)
    rpm = req_count.copy()

    req_count_4xx = np.zeros(row_count, dtype=np.int64)
    req_count_5xx = np.zeros(row_count, dtype=np.int64)
    error_mask = rng.random(row_count) >= 0.956
    if error_mask.any():
        error_size = int(error_mask.sum())
        error_raw = rng.lognormal(mean=math.log(1.0), sigma=1.10, size=error_size)
        tail_mask = rng.random(error_size) < 0.015
        if tail_mask.any():
            error_raw[tail_mask] *= rng.uniform(3.0, 20.0, int(tail_mask.sum()))
        error_upper = np.maximum(1.0, req_count[error_mask] * 0.35)
        total_error = np.rint(np.clip(error_raw, 1.0, error_upper)).astype(np.int64)
        total_error = np.minimum(req_count[error_mask], np.maximum(1, total_error))
        error_4xx = np.rint(total_error * rng.uniform(0.55, 0.95, error_size)).astype(np.int64)
        error_4xx = np.minimum(total_error, np.maximum(0, error_4xx))
        req_count_4xx[error_mask] = error_4xx
        req_count_5xx[error_mask] = total_error - error_4xx

    req_count_error = req_count_4xx + req_count_5xx
    req_count_2xx = req_count - req_count_error
    req_error_rate = req_count_error.astype(float)

    prompt_tokens_avg = sample_lognormal_array(
        rng,
        row_count,
        median=2.30,
        sigma=1.70,
        min_value=0.0,
        max_value=4_779.2,
        zero_prob=0.08,
        tail_prob=0.003,
        tail_low=5.0,
        tail_high=60.0,
    )
    completion_zero_probs = np.where(embedding_like, 0.65, 0.18)
    completion_tokens_avg = sample_lognormal_array(
        rng,
        row_count,
        median=0.178,
        sigma=1.65,
        min_value=0.0,
        max_value=2_880.6,
        zero_prob=completion_zero_probs,
        tail_prob=0.003,
        tail_low=5.0,
        tail_high=70.0,
    )
    prompt_tokens = prompt_tokens_avg * req_count
    completion_tokens = completion_tokens_avg * req_count
    total_tokens = prompt_tokens + completion_tokens

    prompt_tokens_p50, prompt_tokens_p80, prompt_tokens_p90, prompt_tokens_p99, prompt_tokens_max = (
        sample_percentiles_array(
            rng,
            prompt_tokens_avg,
            ((0.45, 0.88), (0.92, 1.78), (1.08, 2.45), (1.75, 5.80), (2.30, 9.50)),
            12_000.0,
        )
    )
    (
        completion_tokens_p50,
        completion_tokens_p80,
        completion_tokens_p90,
        completion_tokens_p99,
        completion_tokens_max,
    ) = sample_percentiles_array(
        rng,
        completion_tokens_avg,
        ((0.35, 0.85), (0.85, 1.75), (1.05, 2.60), (1.70, 6.40), (2.20, 11.0)),
        8_000.0,
    )

    prompt_tpm = sample_lognormal_array(
        rng,
        row_count,
        median=12_395.0,
        sigma=2.32,
        min_value=0.0,
        max_value=76_304_645.0,
        zero_prob=0.018,
        tail_prob=0.010,
        tail_low=3.0,
        tail_high=18.0,
    )
    completion_tpm = sample_lognormal_array(
        rng,
        row_count,
        median=3_044.0,
        sigma=1.48,
        min_value=0.0,
        max_value=18_422_734.0,
        zero_prob=0.08,
        tail_prob=0.008,
        tail_low=3.0,
        tail_high=25.0,
    )
    if embedding_like.any():
        completion_tpm[embedding_like] *= rng.uniform(0.0, 0.08, int(embedding_like.sum()))

    tpm = prompt_tpm + completion_tpm
    prompt_tps = prompt_tpm / 60.0
    completion_tps = completion_tpm / 60.0
    total_tps = prompt_tps + completion_tps

    ttft_avg = np.zeros(row_count, dtype=float)
    ttft_p50 = np.zeros(row_count, dtype=float)
    ttft_p80 = np.zeros(row_count, dtype=float)
    ttft_p90 = np.zeros(row_count, dtype=float)
    ttft_p99 = np.zeros(row_count, dtype=float)
    ttft_max = np.zeros(row_count, dtype=float)
    if stream_bool.any():
        ttft_avg[stream_bool] = sample_lognormal_array(
            rng,
            int(stream_bool.sum()),
            median=1_409.0,
            sigma=1.22,
            min_value=1.0,
            max_value=21_024.0,
            tail_prob=0.006,
            tail_low=1.6,
            tail_high=4.5,
        )
        ttft_percentiles = sample_percentiles_array(
            rng,
            ttft_avg[stream_bool],
            ((0.42, 0.82), (0.88, 1.18), (1.05, 1.46), (1.45, 2.65), (1.80, 3.85)),
            51_198.0,
        )
        for target, values in zip((ttft_p50, ttft_p80, ttft_p90, ttft_p99, ttft_max), ttft_percentiles):
            target[stream_bool] = values

    tpot_zero_probs = np.where(embedding_like, 0.72, 0.22)
    no_completion_mask = (completion_tokens_avg <= 0) & (rng.random(row_count) < 0.70)
    tpot_zero_probs[no_completion_mask] = np.maximum(tpot_zero_probs[no_completion_mask], 0.72)
    tpot_avg = sample_lognormal_array(
        rng,
        row_count,
        median=30.0,
        sigma=0.78,
        min_value=0.0,
        max_value=1_012.6,
        zero_prob=tpot_zero_probs,
        tail_prob=0.006,
        tail_low=2.0,
        tail_high=8.0,
    )
    tpot_p50, tpot_p80, tpot_p90, tpot_p99, tpot_max = sample_percentiles_array(
        rng,
        tpot_avg,
        ((0.58, 0.92), (0.92, 1.18), (1.04, 1.38), (1.35, 2.35), (1.75, 4.20)),
        1_972.0,
    )

    raw_latency = sample_lognormal_array(
        rng,
        row_count,
        median=878.0,
        sigma=1.85,
        min_value=0.0,
        max_value=935_601.0,
        zero_prob=0.03,
        tail_prob=0.010,
        tail_low=2.5,
        tail_high=15.0,
    )
    latency_floor = ttft_avg + tpot_avg * completion_tokens_avg
    latency_avg = raw_latency.copy()
    positive_floor = latency_floor > 0
    if positive_floor.any():
        constrained = np.maximum(
            raw_latency[positive_floor],
            latency_floor[positive_floor] * rng.uniform(1.00, 1.25, int(positive_floor.sum())),
        )
        cap_mask = latency_floor[positive_floor] <= 935_601.0
        constrained[cap_mask] = np.minimum(constrained[cap_mask], 935_601.0)
        latency_avg[positive_floor] = constrained

    instance_count = np.rint(
        sample_lognormal_array(
            rng,
            row_count,
            median=12.0,
            sigma=0.72,
            min_value=1.0,
            max_value=32.0,
            tail_prob=0.02,
            tail_low=1.5,
            tail_high=2.5,
        )
    ).astype(np.int64)
    np.maximum(instance_count, 1, out=instance_count)

    return {
        "service_name": service_name,
        "domain_id": domain_id,
        "card_model": card_model,
        "rpm": rpm,
        "tpm": tpm,
        "ttft_avg": ttft_avg,
        "tpot_avg": tpot_avg,
        "req_count_4xx": req_count_4xx,
        "req_count_5xx": req_count_5xx,
        "req_error_rate": req_error_rate,
        "total_tokens": total_tokens,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "latency_avg": latency_avg,
        "req_count": req_count,
        "req_count_2xx": req_count_2xx,
        "req_count_error": req_count_error,
        "prompt_tokens_avg": prompt_tokens_avg,
        "prompt_tokens_p50": prompt_tokens_p50,
        "prompt_tokens_p80": prompt_tokens_p80,
        "prompt_tokens_p90": prompt_tokens_p90,
        "prompt_tokens_p99": prompt_tokens_p99,
        "prompt_tokens_max": prompt_tokens_max,
        "completion_tokens_avg": completion_tokens_avg,
        "completion_tokens_p50": completion_tokens_p50,
        "completion_tokens_p80": completion_tokens_p80,
        "completion_tokens_p90": completion_tokens_p90,
        "completion_tokens_p99": completion_tokens_p99,
        "completion_tokens_max": completion_tokens_max,
        "ttft_p50": ttft_p50,
        "ttft_p80": ttft_p80,
        "ttft_p90": ttft_p90,
        "ttft_p99": ttft_p99,
        "ttft_max": ttft_max,
        "tpot_p50": tpot_p50,
        "tpot_p80": tpot_p80,
        "tpot_p90": tpot_p90,
        "tpot_p99": tpot_p99,
        "tpot_max": tpot_max,
        "collect_time_std": collect_time_std,
        "prompt_tpm": prompt_tpm,
        "completion_tpm": completion_tpm,
        "infer_service_id": infer_service_id,
        "pool_id": pool_id,
        "region": region,
        "instance_count": instance_count,
        "infer_engine": infer_engine,
        "prompt_tps": prompt_tps,
        "completion_tps": completion_tps,
        "total_tps": total_tps,
        "service_type": service_type,
        "stream": np.where(stream_bool, "true", "false"),
        "sub_models": sub_models,
    }


def parse_datetime(value: str) -> datetime:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    raise argparse.ArgumentTypeError("Use datetime format YYYY-MM-DD HH:MM[:SS].")


def clean_existing_csv(output_dir: Path) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    resolved_dir = output_dir.resolve()
    if resolved_dir == resolved_dir.parent:
        raise ValueError(f"Refusing to clean a filesystem root: {resolved_dir}")

    removed = 0
    for csv_path in output_dir.glob("*.csv"):
        if csv_path.is_file():
            csv_path.unlink()
            removed += 1
    return removed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate fake data2 CSV files matching the 53-column production-like "
            "layout and the 2026-04-02..2026-04-09 minute distribution."
        )
    )
    parser.add_argument("--output-dir", type=Path, default=RAW_DATA_DIR, help="Directory for generated CSV files.")
    parser.add_argument("--total-rows", type=int, default=DEFAULT_TOTAL_ROWS, help="Data rows to generate.")
    parser.add_argument("--file-count", type=int, default=DEFAULT_FILE_COUNT, help="Number of output_part CSV files.")
    parser.add_argument("--start", type=parse_datetime, default=DEFAULT_START, help="Start timestamp.")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="Number of days to cover at minute precision.")
    parser.add_argument("--seed", type=int, default=FAKE_DATA_SEED_BASE, help="Random seed.")
    parser.add_argument(
        "--customer-count",
        type=int,
        default=DEFAULT_CUSTOMER_COUNT,
        help="Global customer id upper bound for domain_id generation.",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=500_000,
        help="Print progress after this many generated rows. Use 0 to disable.",
    )
    parser.add_argument(
        "--chunk-rows",
        type=int,
        default=DEFAULT_CHUNK_ROWS,
        help="Rows generated per vectorized batch.",
    )
    parser.add_argument(
        "--clean-existing-csv",
        action="store_true",
        help="Delete existing non-recursive *.csv files from output-dir before writing.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.total_rows < 0:
        raise ValueError("--total-rows must be >= 0")
    if args.file_count < 1:
        raise ValueError("--file-count must be >= 1")
    if args.days < 1:
        raise ValueError("--days must be >= 1")
    if args.customer_count < 1:
        raise ValueError("--customer-count must be >= 1")
    if args.chunk_rows < 1:
        raise ValueError("--chunk-rows must be >= 1")

    rng = np.random.default_rng(args.seed)
    service_profiles = build_service_profiles()
    runtime_profiles = build_runtime_profiles(service_profiles, rng, args.customer_count)
    service_weights = normalize_weights([profile.row_weight for profile in service_profiles])
    minute_schedule = build_minute_schedule(args.start, args.days, args.total_rows, rng)
    file_counts = largest_remainder_counts(np.ones(args.file_count), args.total_rows).astype(int).tolist()

    nonzero_minutes = sum(1 for _, count in minute_schedule if count > 0)
    minute_counts = [count for _, count in minute_schedule]
    end_time = args.start + timedelta(days=args.days, minutes=-1)
    print(
        "[plan] "
        f"rows={args.total_rows} files={args.file_count} "
        f"time_range={args.start:%Y-%m-%d %H:%M:%S}..{end_time:%Y-%m-%d %H:%M:%S} "
        f"unique_minutes={nonzero_minutes}/{len(minute_schedule)}"
    )
    print(
        "[plan] "
        f"minute_rows_min={min(minute_counts) if minute_counts else 0} "
        f"minute_rows_max={max(minute_counts) if minute_counts else 0} "
        f"file_data_rows_min={min(file_counts) if file_counts else 0} "
        f"file_data_rows_max={max(file_counts) if file_counts else 0} "
        f"chunk_rows={args.chunk_rows}"
    )
    if args.clean_existing_csv:
        removed = clean_existing_csv(args.output_dir)
        print(f"[clean] removed existing csv files={removed} output_dir={args.output_dir}")

    written = 0
    next_progress = args.progress_every if args.progress_every > 0 else 0
    with PartitionedCsvWriter(args.output_dir, file_counts) as writer:
        for collect_time_std in iter_schedule_chunks(minute_schedule, args.chunk_rows):
            columns = generate_chunk_columns(rng, runtime_profiles, service_weights, collect_time_std)
            writer.write_columns(columns)
            written += len(collect_time_std)
            if next_progress and written >= next_progress:
                print(f"[progress] generated rows={written}/{args.total_rows}")
                while written >= next_progress:
                    next_progress += args.progress_every

    print(f"[ok] wrote {args.file_count} files under {args.output_dir} data_rows={written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
