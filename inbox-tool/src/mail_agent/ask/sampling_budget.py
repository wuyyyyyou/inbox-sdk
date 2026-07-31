"""Ask 各阶段的 Sampling token 比例分配。"""

from __future__ import annotations

# Ask 的输出长度随 Host 下发的 maxTokensTotal 调整。主回答失败时只重试原请求，
# 未使用的重试额度不会被预先占用。
ASK_SAMPLING_PHASE_WEIGHTS = {
    "planner": 0.10,
    "answer": 0.70,
    "answer_retry": 0.30,
}

# 没有 Anna sampler 的本地/第三方 provider 也沿用同一比例模型，避免调用方
# 再回退到旧的固定 800 / 1800 / 1024 常量。Anna Host 的单次 8192 上限仍由
# 公共 sampler 最终强制执行；这里仅为不带预算接口的兼容 sampler 提供基数。
_COMPATIBILITY_TOTAL_TOKENS = 32_000
_HOST_SINGLE_CALL_MAX_TOKENS = 8_192
# Ask Answer 需要在较长证据、模型 reasoning 或结构化重试后仍有足够余量闭合 JSON。
# 首次回答与一次格式重试都固定允许 4096，仍低于平台单次 8192 上限。
_ASK_PHASE_TOKEN_CAPS = {
    "planner": 600,
    "answer": 4096,
    "answer_retry": 4096,
}


def ask_answer_output_token_cap(item_limit: int) -> int:
    """Answer 阶段统一允许 4096 输出 token，避免因条目数低估而截断 JSON。"""
    _ = item_limit
    return _ASK_PHASE_TOKEN_CAPS["answer"]


def ask_sampling_tokens(
    sampling_create_message: object | None,
    phase: str,
    *,
    reserve_for: tuple[str, ...] = (),
) -> int:
    """按本次 Host 总授权为 Ask 阶段计算输出额度。

    公共 sampler 会暴露 ``allocate_tokens``，它会同时查看当前剩余额度并为
    后续阶段预留比例，防止 Planner 或首次回答吞掉截断重试所需的额度。普通
    callable stub 和第三方 provider 没有该接口时，以 v1 默认总额度计算，但仍
    遵守单次 8192 的公开平台上限。
    """
    try:
        weight = float(ASK_SAMPLING_PHASE_WEIGHTS[phase])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"unknown Ask sampling phase: {phase}") from exc

    reserve_weights = tuple(
        ASK_SAMPLING_PHASE_WEIGHTS[name]
        for name in reserve_for
        if name in ASK_SAMPLING_PHASE_WEIGHTS
    )
    allocator = getattr(sampling_create_message, "allocate_tokens", None)
    if callable(allocator):
        allocated = allocator(weight=weight, reserve_weights=reserve_weights)
        return max(1, min(int(allocated), _ASK_PHASE_TOKEN_CAPS[phase]))

    return max(1, min(_HOST_SINGLE_CALL_MAX_TOKENS, int(_COMPATIBILITY_TOTAL_TOKENS * weight), _ASK_PHASE_TOKEN_CAPS[phase]))
