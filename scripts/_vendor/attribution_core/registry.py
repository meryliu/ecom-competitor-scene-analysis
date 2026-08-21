"""Shared operator routing and input contracts for the attribution engine."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Optional

REGISTRY_NAME = "ecom-attribution-calculation-engine"
REGISTRY_VERSION = "1.5.0"

FORMULA_METRIC_OBJECTS = ["volume", "ratio"]

COMMON_OUTPUTS = [
    "summary.change_value_or_yoy_trend_delta",
    "rows[].contribution_value",
    "rows[].contribution_rate",
    "rows[].contribution_direction",
    "summary.metric_semantics",
    "ranking",
    "summary.residual",
    "warnings",
    "boundary_cases",
]


def _field(path: str, description: str, required: bool = True, **extra: Any) -> Dict[str, Any]:
    value: Dict[str, Any] = {
        "path": path,
        "required": required,
        "description": description,
    }
    value.update(extra)
    return value


def _metric_change_formula_inputs() -> List[Dict[str, Any]]:
    return [
        _field("metric.name", "目标指标名称", False),
        _field("metric.analysis_value", "分析期整体指标值"),
        _field("metric.comparison_value", "对比期整体指标值"),
        _field("factors[].name", "公式因子名称"),
        _field("factors[].analysis_value", "公式因子分析期值"),
        _field("factors[].comparison_value", "公式因子对比期值"),
    ]


def _yoy_formula_inputs() -> List[Dict[str, Any]]:
    return [
        _field("metric.name", "目标指标名称", False),
        _field("factors[].name", "公式因子名称"),
        _field("factors[].analysis_value", "分析期值"),
        _field("factors[].analysis_last_year_value", "分析期去年同期值"),
        _field("factors[].comparison_value", "对比期值"),
        _field("factors[].comparison_last_year_value", "对比期去年同期值"),
    ]


def _dimension_inputs(yoy: bool = False) -> List[Dict[str, Any]]:
    if yoy:
        return [
            _field("metric.name", "目标指标名称", False),
            _field("groups[].name", "维度值名称"),
            _field("groups[].analysis_value", "分析期维度值"),
            _field("groups[].analysis_last_year_value", "分析期去年同期维度值"),
            _field("groups[].comparison_value", "对比期维度值"),
            _field("groups[].comparison_last_year_value", "对比期去年同期维度值"),
        ]
    return [
        _field("metric.name", "目标指标名称", False),
        _field("groups[].name", "维度值名称"),
        _field("groups[].analysis_value", "分析期维度值"),
        _field("groups[].comparison_value", "对比期维度值"),
        _field("metric.analysis_value", "分析期整体指标值，可由完整分组汇总", False),
        _field("metric.comparison_value", "对比期整体指标值，可由完整分组汇总", False),
    ]


def _structure_inputs(yoy: bool = False) -> List[Dict[str, Any]]:
    if yoy:
        names = [
            "analysis_numerator",
            "analysis_denominator",
            "analysis_last_year_numerator",
            "analysis_last_year_denominator",
            "comparison_numerator",
            "comparison_denominator",
            "comparison_last_year_numerator",
            "comparison_last_year_denominator",
        ]
    else:
        names = [
            "analysis_numerator",
            "analysis_denominator",
            "comparison_numerator",
            "comparison_denominator",
        ]
    fields = [_field("metric.name", "目标比例指标名称", False)]
    fields.extend(_field(f"groups[].{name}", f"维度值的{name}") for name in names)
    fields.extend([
        _field("metric.analysis_value", "分析期整体比例，可由分子分母汇总", False),
        _field("metric.comparison_value", "对比期整体比例，可由分子分母汇总", False),
    ])
    fields.extend(_field(f"metric.{name}", f"父节点完整口径的{name}，用于不闭合组合自动补其他残差", False) for name in names)
    return fields


def _optional_yoy_metric_inputs() -> List[Dict[str, Any]]:
    return [
        _field("metric.analysis_value", "分析期整体值，可由明细汇总", False),
        _field("metric.analysis_last_year_value", "分析期去年同期整体值，可由明细汇总", False),
        _field("metric.comparison_value", "对比期整体值，可由明细汇总", False),
        _field("metric.comparison_last_year_value", "对比期去年同期整体值，可由明细汇总", False),
    ]


def _with_common(
    operator: str,
    scenario: str,
    metric_objects: List[str],
    decompositions: List[str],
    description: str,
    required_inputs: List[Dict[str, Any]],
    constraints: List[str],
    optional_inputs: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    required = [field for field in required_inputs if field.get("required", True)]
    inferred_optional = [field for field in required_inputs if not field.get("required", True)]
    return {
        "operator": operator,
        "scenario": scenario,
        "metric_objects": metric_objects,
        "decompositions": decompositions,
        "supported_target_semantics": (
            ["absolute_delta"]
            if scenario == "metric_change"
            else ["relative_yoy_trend"]
        ),
        "description": description,
        "required_inputs": required,
        "optional_inputs": inferred_optional + (optional_inputs or []) + [
            _field("metric_semantics", "可选指标方向、来源和置信度；仅用于业务解释", False),
            _field("parent_metric_semantics", "可选父目标指标方向；仅用于父子极性推导", False),
            _field("relation_to_parent", "可选父子单调关系 positive/negative/conditional/unknown", False),
            _field("ranking", "可选贡献率 TopN 排序视图；不裁剪或重归一化完整 rows", False),
        ],
        "constraints": constraints,
        "outputs": deepcopy(COMMON_OUTPUTS),
        "execution_protocol": {
            "mode": "python_api",
            "python_api": "attribution_core.run",
            "command": "python3 scripts/attribution_engine.py --input <input.json> --output <output.json>",
            "input_shape": "JSON object matching references/input-contracts.md",
        },
    }


OPERATOR_REGISTRY: Dict[str, Dict[str, Any]] = {
    "additive_change": _with_common(
        "additive_change", "metric_change", FORMULA_METRIC_OBJECTS, ["addition"],
        "加法公式因子对指标变化的贡献",
        _metric_change_formula_inputs(),
        ["公式算子与目标指标是否为比例型无关", "每个因子按完整指标取两期绝对数值", "因子贡献值之和应闭合整体变化", "因子使用 factors 数组"],
        [_field("factors[].sign", "因子符号，默认 +1", False)],
    ),
    "subtractive_change": _with_common(
        "subtractive_change", "metric_change", FORMULA_METRIC_OBJECTS, ["subtraction"],
        "减法公式因子对指标变化的贡献",
        _metric_change_formula_inputs() + [_field("factors[].sign", "加法项 +1，减法项 -1")],
        ["公式算子与目标指标是否为比例型无关", "每个因子按完整指标取两期绝对数值", "因子符号必须为 +1 或 -1", "因子贡献值之和应闭合整体变化"],
    ),
    "multiplicative_change": _with_common(
        "multiplicative_change", "metric_change", FORMULA_METRIC_OBJECTS, ["multiplication"],
        "乘法公式因子贡献，使用 LMDI 对数拆解",
        _metric_change_formula_inputs(),
        ["公式算子与目标指标是否为比例型无关", "每个因子按完整指标取两期绝对数值", "整体值和所有因子两期值必须大于 0", "因子使用 factors 数组"],
        [_field("factors[].role", "默认 multiplier", False)],
    ),
    "division_change": _with_common(
        "division_change", "metric_change", FORMULA_METRIC_OBJECTS, ["division"],
        "除法公式拆解，分母因子按倒数方向计算",
        _metric_change_formula_inputs() + [_field("factors[].role", "numerator/multiplier/denominator/divisor")],
        ["公式算子与目标指标是否为比例型无关", "每个因子按完整指标取两期绝对数值", "整体值和所有因子两期值必须大于 0", "至少有一个 denominator 或 divisor 因子"],
    ),
    "dimension_change": _with_common(
        "dimension_change", "metric_change", ["volume"], ["dimension"],
        "量级指标按维度值拆解指标变化",
        _dimension_inputs(),
        ["分组值覆盖完整或显式标记 partial_coverage", "不闭合组合必须提供父节点完整口径并自动补其他残差", "部分维值贡献率使用大盘整体变化作分母"],
        [_field("partial_coverage", "是否只覆盖部分维值", False), _field("coverage", "覆盖与残差配置", False)],
    ),
    "structure_change": _with_common(
        "structure_change", "metric_change", ["ratio"], ["structure"],
        "比例指标按维度拆解自身变化和结构占比变化",
        _structure_inputs(),
        ["父节点总体分母必须大于 0", "分组零分母按 sparse_policy 先合并其他、合并残差，再执行配对级 epsilon", "不闭合组合必须提供父节点完整口径，不得静默重归一化", "epsilon 平滑必须显式标注近似口径"],
        [
            _field("sparse_strategy", "other/rollup/epsilon_smoothing", False),
            _field("epsilon", "epsilon 平滑值", False),
            _field("reference_rate_policy", "新增/消失组合的参考率策略", False),
            _field("coverage", "覆盖与残差配置", False),
            _field("sparse_policy", "Query 级稀疏组合合并、上卷和 epsilon 策略", False),
        ],
    ),
    "dimension_yoy_trend": _with_common(
        "dimension_yoy_trend", "yoy_trend_change", ["volume"], ["dimension"],
        "量级指标维度值对同比趋势变化的贡献",
        _dimension_inputs(yoy=True),
        ["两个去年同期总体值不能为 0", "需要分析期、分析期去年同期、对比期、对比期去年同期四组值", "不闭合组合必须提供父节点完整口径并自动补其他残差"],
        _optional_yoy_metric_inputs() + [_field("coverage", "覆盖与残差配置", False)],
    ),
    "structure_yoy_trend": _with_common(
        "structure_yoy_trend", "yoy_trend_change", ["ratio"], ["structure", "dimension"],
        "比例指标结构对同比趋势变化的贡献",
        _structure_inputs(yoy=True),
        ["四个父节点总体分母必须大于 0", "分组零分母按两个同比配对分别处理", "不闭合组合必须提供父节点完整口径并自动补其他残差", "epsilon 只在配对内引用可观测自身率"],
        _optional_yoy_metric_inputs() + [
            _field("coverage", "覆盖与残差配置", False),
            _field("sparse_policy", "Query 级稀疏组合合并、上卷和 epsilon 策略", False),
        ],
    ),
    "additive_yoy_trend": _with_common(
        "additive_yoy_trend", "yoy_trend_change", FORMULA_METRIC_OBJECTS, ["addition"],
        "加法公式因子对同比趋势变化的贡献",
        _yoy_formula_inputs(),
        ["公式算子与目标指标是否为比例型无关", "每个因子按完整指标取四期绝对数值", "两个去年同期总体值不能为 0", "因子贡献应闭合同比趋势变化"],
        _optional_yoy_metric_inputs(),
    ),
    "subtractive_yoy_trend": _with_common(
        "subtractive_yoy_trend", "yoy_trend_change", FORMULA_METRIC_OBJECTS, ["subtraction"],
        "减法公式因子对同比趋势变化的贡献",
        _yoy_formula_inputs() + [_field("factors[].sign", "加法项 +1，减法项 -1")],
        ["公式算子与目标指标是否为比例型无关", "每个因子按完整指标取四期绝对数值", "两个去年同期总体值不能为 0", "因子符号必须为 +1 或 -1"],
        _optional_yoy_metric_inputs(),
    ),
    "multiplicative_yoy_trend": _with_common(
        "multiplicative_yoy_trend", "yoy_trend_change", FORMULA_METRIC_OBJECTS, ["multiplication"],
        "乘法公式因子对同比趋势变化的贡献",
        _yoy_formula_inputs(),
        ["公式算子与目标指标是否为比例型无关", "每个因子按完整指标取四期绝对数值", "所有因子本期和去年同期值必须大于 0", "metric 四组总体值可选，缺省时由因子同比倍数推导"],
        [
            _field("metric.analysis_value", "分析期整体值", False),
            _field("metric.analysis_last_year_value", "分析期去年同期整体值", False),
            _field("metric.comparison_value", "对比期整体值", False),
            _field("metric.comparison_last_year_value", "对比期去年同期整体值", False),
            _field("factors[].role", "默认 multiplier", False),
        ],
    ),
    "division_yoy_trend": _with_common(
        "division_yoy_trend", "yoy_trend_change", FORMULA_METRIC_OBJECTS, ["division"],
        "除法公式因子对同比趋势变化的贡献",
        _yoy_formula_inputs() + [_field("factors[].role", "至少一个 denominator 或 divisor")],
        ["公式算子与目标指标是否为比例型无关", "每个因子按完整指标取四期绝对数值", "所有因子本期和去年同期值必须大于 0", "至少有一个 denominator 或 divisor 因子"],
    ),
}


def get_operator(operator: str) -> Optional[Dict[str, Any]]:
    definition = OPERATOR_REGISTRY.get(operator)
    return deepcopy(definition) if definition else None


def route_operator(scenario: str, metric_object: str, decomposition: str) -> str:
    if scenario == "metric_change" and decomposition == "dimension" and metric_object == "ratio":
        return "structure_change"
    if scenario == "yoy_trend_change" and decomposition == "dimension" and metric_object == "ratio":
        return "structure_yoy_trend"
    for operator, definition in OPERATOR_REGISTRY.items():
        if (
            definition["scenario"] == scenario
            and metric_object in definition["metric_objects"]
            and decomposition in definition["decompositions"]
        ):
            return operator
    raise ValueError(
        f"unsupported attribution route: scenario={scenario!r}, metric_object={metric_object!r}, decomposition={decomposition!r}"
    )


def operator_matches(operator: str, scenario: Optional[str], metric_object: Optional[str], decomposition: Optional[str]) -> bool:
    definition = OPERATOR_REGISTRY.get(operator)
    if not definition:
        return False
    if scenario and definition["scenario"] != scenario:
        return False
    if metric_object and metric_object not in definition["metric_objects"]:
        return False
    if decomposition and decomposition not in definition["decompositions"]:
        return False
    return True
