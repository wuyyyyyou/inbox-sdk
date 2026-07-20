"""Ask 各阶段的 Sampling token 比例分配。"""

from __future__ import annotations

# Ask 的输出长度随 Host 下发的 maxTokensTotal 调整。比例之和为 100%，
# 其中重试与 JSON 修复只在对应失败分支发生，未使用的额度不会被预先占用。
ASK_SAMPLING_PHASE_WEIGHTS = {
    "planner": 0.10,
    "answer": 0.60,
    "answer_retry": 0.25,
    "json_repair": 0.05,
}

# 没有 Anna sampler 的本地/第三方 provider 也沿用同一比例模型，避免调用方
# 再回退到旧的固定 800 / 1800 / 1024 常量。Anna Host 的单次 8192 上限仍由
# 公共 sampler 最终强制执行；这里仅为不带预算接口的兼容 sampler 提供基数。
_COMPATIBILITY_TOTAL_TOKENS = 32_000
_HOST_SINGLE_CALL_MAX_TOKENS = 8_192
# 邮箱问答只需少量优先事项，但仍须为 Host 侧 thinking/推理模型预留输出空间：
# 若硬上限过低，模型可能把额度耗在推理正文上，最终回答里连 `{` 都没有。
# 同时继续远低于 8192 单次平台上限，避免单阶段吞掉整笔 grant。
_ASK_PHASE_TOKEN_CAPS = {
    "planner": 600,
    "answer": 4096,
    "answer_retry": 4096,
    "json_repair": 800,
}


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
