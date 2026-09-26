"""模型路由 / 能力校验：不要因为库里有模型名就假定可用。"""
from .. import registry
from ..generators.image import ADAPTER_TASKS

LOCAL_IDS = ("local-poster-compositor", "demo-poster-compositor")


def validate_model_for_task(model_id: str, task_type: str, reference_count: int = 0, ratio: str | None = None, count: int | None = None) -> dict:
    m = registry.get_model(model_id)
    if not m:
        raise ValueError(f"未知模型：{model_id}")
    if int(m.get("enabled") or 0) != 1:
        raise ValueError(f"模型未启用：{model_id}")
    adapter = m.get("adapter")
    if model_id not in LOCAL_IDS and adapter and task_type not in ADAPTER_TASKS.get(adapter, []):
        raise ValueError(f"模型 {model_id} 不支持任务类型 {task_type}（支持：{ADAPTER_TASKS.get(adapter, [])}）")
    cap = registry.capabilities(model_id)
    ratios = cap.get("aspect_ratios") or cap.get("supported_ratios") or []
    if ratio and ratios and ratio not in ratios:
        raise ValueError(f"模型 {model_id} 不支持比例 {ratio}（支持：{ratios}）")
    maxc = cap.get("max_count") or cap.get("max_image_count") or 1
    if count and int(count) > int(maxc):
        raise ValueError(f"张数超出模型上限 {maxc}")
    limit = cap.get("image_input_limit") or cap.get("reference_image_limit") or 0
    if reference_count and int(reference_count) > int(limit):
        raise ValueError(f"参考图数量超出模型上限 {limit}")
    return m
