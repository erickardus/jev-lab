"""Jev usage -> dollars, so every example can print what a run cost.

Jev bills input tokens only; output is free. Price from https://docs.typesafe.ai/models
(jev-1.13, checked 2026-09-19). Override with JEV_USD_PER_MTOK if it changes.
"""

from __future__ import annotations

import os

USD_PER_MTOK = float(os.environ.get("JEV_USD_PER_MTOK", "0.042"))


def cost_usd(input_tokens: int) -> float:
    return input_tokens / 1_000_000 * USD_PER_MTOK


def format_usage(requests: int, input_tokens: int, seconds: float | None = None) -> str:
    """'3 request(s) · 12,345 input tokens · $0.0005 · 1.8s'"""
    parts = [f"{requests} request(s)", f"{input_tokens:,} input tokens", f"${cost_usd(input_tokens):.4f}"]
    if seconds is not None:
        parts.append(f"{seconds:.1f}s")
    return " · ".join(parts)
