"""LLM 成本计算（人民币）。

价格单位：每 100 万 token 的人民币单价。来源为 DeepSeek 官方定价（会变动，
改这里的常量即可）。
"""

PRICING: dict[str, dict[str, float]] = {
    # 模型名: {"prompt": 输入价, "completion": 输出价}（元 / 百万 token）
    "deepseek-chat": {"prompt": 2.0, "completion": 8.0},
    "deepseek-reasoner": {"prompt": 4.0, "completion": 16.0},
}
DEFAULT_PRICE = {"prompt": 2.0, "completion": 8.0}


def compute_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """按 token 用量计算单次调用成本（人民币元）。"""
    p = PRICING.get(model, DEFAULT_PRICE)
    return (
        prompt_tokens / 1_000_000 * p["prompt"]
        + completion_tokens / 1_000_000 * p["completion"]
    )
