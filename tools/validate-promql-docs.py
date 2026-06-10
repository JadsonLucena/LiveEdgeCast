#!/usr/bin/env python3
"""Static sanity checks for observability PromQL/LogQL documentation snippets.

This script intentionally uses only the Python standard library. It is not a full
PromQL parser; it catches repository-specific regressions that previously made
canonical documentation snippets misleading or syntactically invalid. If
`promtool` is available in the execution environment, run it against rendered
rules/dashboards as an additional parser-level validation step.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMQL_DOC = ROOT / "docs" / "observability" / "promql.md"
METRICS_DOC = ROOT / "docs" / "observability" / "metrics.md"
DOCS = (PROMQL_DOC, METRICS_DOC)

FENCE_RE = re.compile(r"```(?P<lang>\w+)?\n(?P<body>.*?)```", re.DOTALL)


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def check_fences(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    fence_count = text.count("```")
    if fence_count % 2:
        fail(f"{path.relative_to(ROOT)} has unbalanced Markdown fences ({fence_count})")


def extract_blocks(path: Path, language: str) -> list[tuple[int, str]]:
    text = path.read_text(encoding="utf-8")
    blocks: list[tuple[int, str]] = []
    for match in FENCE_RE.finditer(text):
        if (match.group("lang") or "").lower() == language:
            blocks.append((line_number(text, match.start()), match.group("body")))
    return blocks


def check_no_range_selector_on_parenthesized_expression(blocks: list[tuple[int, str]]) -> None:
    invalid = re.compile(r"\([^\n]*[-+*/][^\n]*\)\s*\[[0-9]+[smhdwy](?::[^\]]*)?\]")
    for start_line, body in blocks:
        match = invalid.search(body)
        if match:
            snippet_line = start_line + body[: match.start()].count("\n") + 1
            fail(
                "range selector appears to be applied to a parenthesized expression "
                f"near {PROMQL_DOC.relative_to(ROOT)}:{snippet_line}; use a subquery [range:] "
                "or put the range on the vector selector"
            )


def check_worker_ffmpeg_scoped(blocks: list[tuple[int, str]]) -> None:
    unscoped = re.compile(r"worker_ffmpeg_health_state(?!\s*\{)")
    for start_line, body in blocks:
        match = unscoped.search(body)
        if match:
            snippet_line = start_line + body[: match.start()].count("\n") + 1
            fail(
                f"unscoped worker_ffmpeg_health_state near {PROMQL_DOC.relative_to(ROOT)}:{snippet_line}; "
                "include namespace/job labels to avoid cross-environment aggregation"
            )


def check_ratio_denominators_are_clamped(blocks: list[tuple[int, str]]) -> None:
    for start_line, body in blocks:
        if "/" not in body:
            continue
        lines = body.splitlines()
        for idx, line in enumerate(lines):
            if line.strip() != "/":
                continue
            denominator = lines[idx + 1].strip() if idx + 1 < len(lines) else ""
            if denominator.startswith("clamp_min("):
                continue
            # Percent normalization by an intentionally non-zero or separately clamped gauge can be valid,
            # but the current canonical ratio snippets should clamp direct counter denominators.
            if denominator.startswith("sum(rate("):
                fail(
                    f"unclamped rate denominator near {PROMQL_DOC.relative_to(ROOT)}:{start_line + idx + 1}; "
                    "wrap it with clamp_min(..., 1e-9) for idle windows"
                )


def check_cadvisor_container_filters(blocks: list[tuple[int, str]]) -> None:
    for start_line, body in blocks:
        if "container_cpu_usage_seconds_total" in body and 'container!="POD"' not in body:
            fail(
                f"CPU cAdvisor query near {PROMQL_DOC.relative_to(ROOT)}:{start_line} "
                "should exclude container=\"POD\""
            )
        if "container_memory_working_set_bytes" in body and 'container!="POD"' not in body:
            fail(
                f"memory cAdvisor query near {PROMQL_DOC.relative_to(ROOT)}:{start_line} "
                "should exclude container=\"POD\""
            )
        if any(metric in body for metric in (
            "container_cpu_usage_seconds_total",
            "container_memory_working_set_bytes",
            "container_network_receive_bytes_total",
            "container_network_transmit_bytes_total",
        )):
            if "proxy-lb|proxy|worker|controller" not in body:
                fail(
                    f"resource rollup near {PROMQL_DOC.relative_to(ROOT)}:{start_line} "
                    "must split proxy-lb from proxy so HAProxy is not counted as RTMP proxy"
                )


def check_non_ready_query_uses_kube_state_metrics(blocks: list[tuple[int, str]]) -> None:
    for start_line, body in blocks:
        if "pod_ready_status" in body and ("max_over_time" in body or "min_over_time" in body):
            fail(
                f"non-ready-over-time query near {PROMQL_DOC.relative_to(ROOT)}:{start_line} "
                "must use kube-state-metrics readiness so deleted Pods go stale"
            )
        if "kube_pod_status_ready" not in body:
            continue
        if "kube_pod_created" not in body or "> bool 300" not in body:
            fail(
                f"non-ready-over-time query near {PROMQL_DOC.relative_to(ROOT)}:{start_line} "
                "must require Pod age > 300s so normal cold-starting workers are not counted"
            )
        if 'condition="false"' in body and "unknown" not in body:
            fail(
                f"non-ready-over-time query near {PROMQL_DOC.relative_to(ROOT)}:{start_line} "
                "must treat Ready=Unknown as non-ready too, not only Ready=False"
            )
        if 'condition="true"' in body and "1 - max_over_time" not in body:
            fail(
                f"non-ready-over-time query near {PROMQL_DOC.relative_to(ROOT)}:{start_line} "
                "should count absence of Ready=True over the window so false and unknown are both non-ready"
            )


def check_missing_worker_health_coalesces_to_zero(blocks: list[tuple[int, str]]) -> None:
    for start_line, body in blocks:
        if (
            "kube_pod_status_phase" in body
            and "worker_ffmpeg_health_state" in body
            and "-" in body
            and "or on() vector(0)" not in body
        ):
            fail(
                f"worker health subtraction near {PROMQL_DOC.relative_to(ROOT)}:{start_line} "
                "must coalesce missing worker_ffmpeg_health_state series with or on() vector(0)"
            )


def check_allocation_replay_filter(blocks: list[tuple[int, str]]) -> None:
    for start_line, body in blocks:
        if "stream_allocation_total" not in body or "idempotent_replay" not in body:
            continue
        if "concurrent_idempotent_replay" not in body:
            fail(
                f"allocation success query near {PROMQL_DOC.relative_to(ROOT)}:{start_line} "
                "must exclude both idempotent_replay and concurrent_idempotent_replay"
            )


def check_handover_denominator_guidance(blocks: list[tuple[int, str]]) -> None:
    text = PROMQL_DOC.read_text(encoding="utf-8")
    if "não é denominador de taxa de aceite de handover" not in text:
        fail("handover docs must state handover_attempts_total is not the effective handover acceptance denominator")
    if "stream_proxy_handover_total + handover_conflict_total" not in text:
        fail("handover docs must define real owner-change denominator as stream_proxy_handover_total + handover_conflict_total")
    if "não mede taxa de aceite de handover efetivo" not in text:
        fail("handover docs must label handover_attempts_total ratios as ownership-evaluation normalization only")

    has_owner_change_ratio = any(
        "stream_proxy_handover_total" in body
        and "handover_conflict_total" in body
        and "+" in body
        and "/" in body
        for _, body in blocks
    )
    if not has_owner_change_ratio:
        fail("handover docs must include an acceptance ratio over real owner-change events")


def check_worker_pods_available_wording() -> None:
    text = METRICS_DOC.read_text(encoding="utf-8")
    if "não usar como capacidade livre de alocação" not in text:
        fail("metrics catalog must state worker_pods_available is not free allocation capacity")


def main() -> int:
    for doc in DOCS:
        check_fences(doc)
    promql_blocks = extract_blocks(PROMQL_DOC, "promql")
    if not promql_blocks:
        fail(f"no promql blocks found in {PROMQL_DOC.relative_to(ROOT)}")
    check_no_range_selector_on_parenthesized_expression(promql_blocks)
    check_worker_ffmpeg_scoped(promql_blocks)
    check_ratio_denominators_are_clamped(promql_blocks)
    check_cadvisor_container_filters(promql_blocks)
    check_non_ready_query_uses_kube_state_metrics(promql_blocks)
    check_missing_worker_health_coalesces_to_zero(promql_blocks)
    check_allocation_replay_filter(promql_blocks)
    check_handover_denominator_guidance(promql_blocks)
    check_worker_pods_available_wording()
    print(f"Validated {len(promql_blocks)} PromQL snippets in {PROMQL_DOC.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
