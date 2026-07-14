"""Ask 各阶段的输出 token 额度。"""

from __future__ import annotations

# 中文注释：累计额度、单次上限和超时由 anna_inbox_executa.sampling_tools
# 的公共 sampler 统一执行，避免 Ask 与其他工作流采用不同的 Host 预算语义。
ASK_PLANNER_MAX_TOKENS = 512
ASK_ANSWER_MAX_TOKENS = 1536
ASK_JSON_REPAIR_MAX_TOKENS = 512
