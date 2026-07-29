"""Validate professional parameter translations and grounded AI analysis."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any


PARAMETER_ANALYSIS_SCHEMA_VERSION = 1
PARAMETER_ANALYSIS_PROMPT_VERSION = "pv-parameter-analysis-v2"
PARAMETER_GLOSSARY_VERSION = "pv-zh-technical-v1"
MAX_ANALYSIS_PARAMETERS = 200
MAX_ANALYSIS_INPUT_CHARS = 70_000

ANALYSIS_SECTION_TITLES = {
    "product_positioning": "产品定位与功率配置",
    "dc_input": "直流输入与 MPPT",
    "ac_output": "交流输出与电网侧",
    "efficiency": "效率表现",
    "protection": "保护功能与电气安全",
    "installation": "安装、环境与运维",
    "limitations": "数据边界与选型注意事项",
}
ANALYSIS_KINDS = frozenset(
    {
        "engineering_interpretation",
        "conditional_guidance",
        "limitation",
    }
)

SECTION_TRANSLATIONS = {
    "input (dc)": "直流输入（DC）",
    "input ( dc)": "直流输入（DC）",
    "output (ac)": "交流输出（AC）",
    "output ( ac)": "交流输出（AC）",
    "efficiency": "效率",
    "protection": "保护功能",
    "interface": "接口与通信",
    "communication": "通信",
    "general data": "常规参数",
    "environmental data": "环境参数",
    "mechanical data": "机械参数",
}

_REQUIRED_TERMS = (
    (re.compile(r"\bMax\.?\b", re.IGNORECASE), "最大"),
    (re.compile(r"\bMin\.?\b", re.IGNORECASE), "最小"),
    (re.compile(r"\bRated\b", re.IGNORECASE), "额定"),
    (re.compile(r"\bNominal\b", re.IGNORECASE), "标称"),
    (re.compile(r"\bVoltage\b", re.IGNORECASE), "电压"),
    (re.compile(r"\bCurrent\b", re.IGNORECASE), "电流"),
    (re.compile(r"\bPower\b", re.IGNORECASE), "功率"),
    (re.compile(r"\bRange\b", re.IGNORECASE), "范围"),
    (re.compile(r"\bEfficiency\b", re.IGNORECASE), "效率"),
    (re.compile(r"\bProtection\b", re.IGNORECASE), "保护"),
    (re.compile(r"\bFrequency\b", re.IGNORECASE), "频率"),
    (re.compile(r"\bTemperature\b", re.IGNORECASE), "温度"),
    (re.compile(r"\bCooling\b", re.IGNORECASE), "冷却"),
    (re.compile(r"\bHumidity\b", re.IGNORECASE), "湿度"),
    (re.compile(r"\bAltitude\b", re.IGNORECASE), "海拔"),
    (re.compile(r"\bNoise\b", re.IGNORECASE), "噪声"),
    (re.compile(r"\bDimensions?\b", re.IGNORECASE), "尺寸"),
    (re.compile(r"\bWeight\b", re.IGNORECASE), "重量"),
    (re.compile(r"\bWarranty\b", re.IGNORECASE), "质保"),
)
_PROTECTED_TOKEN_RE = re.compile(
    r"\[[^\[\]\r\n]{1,24}\]"
    r"|(?<![A-Za-z0-9])(?:"
    r"MPPT|THD[iI]?|DCI|GFCI|AFCI|STC|NMOT|"
    r"RS\d+|Wi-Fi|GPRS|[345]G|IP\d+"
    r")(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])\d+(?:\.\d+)?(?![A-Za-z0-9])"
)
_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])\d+(?:\.\d+)?(?![A-Za-z0-9])")
_HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_MARKDOWN_OR_HTML_RE = re.compile(
    r"(?:^|\n)\s{0,3}(?:#{1,6}|[-*+]\s|>\s|```)|<[^>\r\n]+>",
    re.MULTILINE,
)


class ParameterAnalysisError(ValueError):
    """Raised when translations or analysis cannot be safely published."""


def parameter_id(index: int) -> str:
    """Return the stable positional ID used only within one parameter set."""

    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise ValueError("parameter index must be a non-negative integer")
    return f"p{index + 1:03d}"


def _clean_text(
    value: Any,
    field: str,
    *,
    required: bool = False,
    limit: int,
    require_han: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ParameterAnalysisError(f"{field} must be a string")
    cleaned = " ".join(value.replace("\x00", "").split())
    if required and not cleaned:
        raise ParameterAnalysisError(f"{field} is required")
    if len(cleaned) > limit:
        raise ParameterAnalysisError(f"{field} exceeds {limit} characters")
    if cleaned and require_han and _HAN_RE.search(cleaned) is None:
        raise ParameterAnalysisError(f"{field} must contain professional Chinese")
    if cleaned and _MARKDOWN_OR_HTML_RE.search(cleaned):
        raise ParameterAnalysisError(f"{field} cannot contain Markdown or HTML")
    return cleaned


def _as_parameter_rows(
    parameters: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if isinstance(parameters, (str, bytes, bytearray)) or not isinstance(
        parameters, Sequence
    ):
        raise TypeError("parameters must be a sequence")
    if not 1 <= len(parameters) <= MAX_ANALYSIS_PARAMETERS:
        raise ParameterAnalysisError(
            f"parameters must contain 1-{MAX_ANALYSIS_PARAMETERS} rows"
        )
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(parameters):
        if not isinstance(item, Mapping):
            raise ParameterAnalysisError(f"parameters[{index}] must be an object")
        name = _clean_text(
            item.get("name"),
            f"parameters[{index}].name",
            required=True,
            limit=200,
        )
        value = item.get("value")
        if value is None or isinstance(value, (dict, list)):
            raise ParameterAnalysisError(
                f"parameters[{index}].value must be scalar"
            )
        if isinstance(value, float) and not math.isfinite(value):
            raise ParameterAnalysisError(
                f"parameters[{index}].value must be finite"
            )
        normalized.append(
            {
                "parameter_id": parameter_id(index),
                "name": name,
                "section": _clean_text(
                    item.get("section", ""),
                    f"parameters[{index}].section",
                    limit=100,
                ),
                "subsection": _clean_text(
                    item.get("subsection", ""),
                    f"parameters[{index}].subsection",
                    limit=100,
                ),
                "value": value,
                "unit": _clean_text(
                    item.get("unit", ""),
                    f"parameters[{index}].unit",
                    limit=80,
                ),
            }
        )
    return normalized


def parameter_analysis_input(
    parameters: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return the complete bounded parameter set sent to the model."""

    rows = _as_parameter_rows(parameters)
    encoded = json.dumps(
        rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(encoded) > MAX_ANALYSIS_INPUT_CHARS:
        raise ParameterAnalysisError(
            "complete parameter input exceeds the analysis character budget"
        )
    return rows


def parameter_set_sha256(
    parameters: Sequence[Mapping[str, Any]],
) -> str:
    """Hash the exact normalized rows used for translation and analysis."""

    encoded = json.dumps(
        parameter_analysis_input(parameters),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_translation(
    raw: Mapping[str, Any],
    source: Mapping[str, Any],
    *,
    index: int,
) -> dict[str, str]:
    prefix = f"translations[{index}]"
    allowed = {
        "parameter_id",
        "name_zh",
        "section_zh",
        "subsection_zh",
        "value_zh",
    }
    if set(raw) - allowed:
        raise ParameterAnalysisError(f"{prefix} has unknown fields")
    if raw.get("parameter_id") != source["parameter_id"]:
        raise ParameterAnalysisError(
            f"{prefix}.parameter_id must match the complete input order"
        )
    name_zh = _clean_text(
        raw.get("name_zh"),
        f"{prefix}.name_zh",
        required=True,
        limit=200,
        require_han=True,
    )
    source_name = str(source["name"])
    for pattern, required_term in _REQUIRED_TERMS:
        if pattern.search(source_name) and required_term not in name_zh:
            raise ParameterAnalysisError(
                f"{prefix}.name_zh must preserve the controlled term "
                f"{required_term}"
            )
    for token in _PROTECTED_TOKEN_RE.findall(source_name):
        if token not in name_zh:
            raise ParameterAnalysisError(
                f"{prefix}.name_zh must preserve protected token {token}"
            )

    source_section = str(source["section"])
    section_zh = _clean_text(
        raw.get("section_zh", ""),
        f"{prefix}.section_zh",
        required=bool(source_section),
        limit=100,
        require_han=bool(source_section),
    )
    expected_section = SECTION_TRANSLATIONS.get(source_section.casefold())
    if expected_section is not None and section_zh != expected_section:
        raise ParameterAnalysisError(
            f"{prefix}.section_zh must use the controlled section translation"
        )
    for token in _PROTECTED_TOKEN_RE.findall(source_section):
        if token not in section_zh:
            raise ParameterAnalysisError(
                f"{prefix}.section_zh must preserve protected token {token}"
            )

    source_subsection = str(source["subsection"])
    subsection_zh = _clean_text(
        raw.get("subsection_zh", ""),
        f"{prefix}.subsection_zh",
        required=bool(source_subsection),
        limit=100,
        require_han=bool(source_subsection),
    )
    expected_subsection = SECTION_TRANSLATIONS.get(source_subsection.casefold())
    if expected_subsection is not None and subsection_zh != expected_subsection:
        raise ParameterAnalysisError(
            f"{prefix}.subsection_zh must use the controlled section "
            "translation"
        )
    for token in _PROTECTED_TOKEN_RE.findall(source_subsection):
        if token not in subsection_zh:
            raise ParameterAnalysisError(
                f"{prefix}.subsection_zh must preserve protected token {token}"
            )

    value_zh = _clean_text(
        raw.get("value_zh", ""),
        f"{prefix}.value_zh",
        limit=200,
        require_han=bool(raw.get("value_zh")),
    )
    source_value = str(source["value"])
    for token in _PROTECTED_TOKEN_RE.findall(source_value):
        if token not in value_zh and value_zh:
            raise ParameterAnalysisError(
                f"{prefix}.value_zh must preserve protected token {token}"
            )
    return {
        "parameter_id": str(source["parameter_id"]),
        "name_zh": name_zh,
        "section_zh": section_zh,
        "subsection_zh": subsection_zh,
        "value_zh": value_zh,
    }


def _string_list(
    value: Any,
    field: str,
    *,
    limit: int,
    item_limit: int,
) -> list[str]:
    if not isinstance(value, list) or len(value) > limit:
        raise ParameterAnalysisError(f"{field} must be an array of at most {limit}")
    return [
        _clean_text(
            item,
            f"{field}[{index}]",
            required=True,
            limit=item_limit,
            require_han=True,
        )
        for index, item in enumerate(value)
    ]


def _ground_numbers(
    text_values: Sequence[str],
    basis_rows: Sequence[Mapping[str, Any]],
    field: str,
) -> None:
    used = _NUMBER_RE.findall(" ".join(text_values))
    if not used:
        return
    source_text = " ".join(
        f"{item['name']} {item['value']} {item['unit']}" for item in basis_rows
    )
    supported = set(_NUMBER_RE.findall(source_text))
    unsupported = [number for number in used if number not in supported]
    if unsupported:
        raise ParameterAnalysisError(
            f"{field} contains numeric text not present in its basis parameters"
        )


def validate_parameter_enrichment(
    raw: Mapping[str, Any],
    parameters: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Validate complete translations and basis-bound professional analysis."""

    if not isinstance(raw, Mapping):
        raise ParameterAnalysisError("parameter enrichment must be an object")
    metadata_fields = {
        "schema_version",
        "prompt_version",
        "glossary_version",
        "input_parameter_count",
        "input_complete",
    }
    if set(raw) - {
        "translations",
        "sections",
        "overall_limitations_zh",
        *metadata_fields,
    }:
        raise ParameterAnalysisError("parameter enrichment has unknown fields")
    source_rows = parameter_analysis_input(parameters)
    expected_metadata = {
        "schema_version": PARAMETER_ANALYSIS_SCHEMA_VERSION,
        "prompt_version": PARAMETER_ANALYSIS_PROMPT_VERSION,
        "glossary_version": PARAMETER_GLOSSARY_VERSION,
        "input_parameter_count": len(source_rows),
        "input_complete": True,
    }
    for key in metadata_fields:
        if key in raw and raw[key] != expected_metadata[key]:
            raise ParameterAnalysisError(
                f"parameter enrichment {key} does not match its input"
            )
    translations_raw = raw.get("translations")
    if (
        not isinstance(translations_raw, list)
        or len(translations_raw) != len(source_rows)
    ):
        raise ParameterAnalysisError(
            "translations must contain exactly one item for every parameter"
        )
    translations: list[dict[str, str]] = []
    section_translations: dict[str, str] = {}
    subsection_translations: dict[str, str] = {}
    for index, (translation_raw, source) in enumerate(
        zip(translations_raw, source_rows, strict=True)
    ):
        if not isinstance(translation_raw, Mapping):
            raise ParameterAnalysisError(
                f"translations[{index}] must be an object"
            )
        translation = _validate_translation(
            translation_raw,
            source,
            index=index,
        )
        for source_key, translated_key, memory in (
            ("section", "section_zh", section_translations),
            ("subsection", "subsection_zh", subsection_translations),
        ):
            source_value = str(source[source_key])
            translated_value = translation[translated_key]
            if not source_value:
                continue
            normalized_key = unicodedata.normalize(
                "NFKC", source_value
            ).casefold()
            previous = memory.setdefault(normalized_key, translated_value)
            if previous != translated_value:
                raise ParameterAnalysisError(
                    f"{translated_key} must be consistent for repeated headings"
                )
        translations.append(translation)

    sections_raw = raw.get("sections")
    minimum_sections = 3 if len(source_rows) >= 10 else 1
    if (
        not isinstance(sections_raw, list)
        or not minimum_sections <= len(sections_raw) <= len(
            ANALYSIS_SECTION_TITLES
        )
    ):
        raise ParameterAnalysisError(
            f"sections must contain {minimum_sections}-"
            f"{len(ANALYSIS_SECTION_TITLES)} entries"
        )
    source_by_id = {str(item["parameter_id"]): item for item in source_rows}
    seen_sections: set[str] = set()
    normalized_sections: list[dict[str, Any]] = []
    for section_index, section_raw in enumerate(sections_raw):
        prefix = f"sections[{section_index}]"
        if not isinstance(section_raw, Mapping):
            raise ParameterAnalysisError(f"{prefix} must be an object")
        if set(section_raw) - {"section_code", "paragraphs"}:
            raise ParameterAnalysisError(f"{prefix} has unknown fields")
        section_code = section_raw.get("section_code")
        if (
            not isinstance(section_code, str)
            or section_code not in ANALYSIS_SECTION_TITLES
            or section_code in seen_sections
        ):
            raise ParameterAnalysisError(
                f"{prefix}.section_code must be a unique documented code"
            )
        seen_sections.add(section_code)
        paragraphs_raw = section_raw.get("paragraphs")
        if not isinstance(paragraphs_raw, list) or not 1 <= len(
            paragraphs_raw
        ) <= 2:
            raise ParameterAnalysisError(
                f"{prefix}.paragraphs must contain 1-2 entries"
            )
        paragraphs: list[dict[str, Any]] = []
        for paragraph_index, paragraph_raw in enumerate(paragraphs_raw):
            paragraph_prefix = (
                f"{prefix}.paragraphs[{paragraph_index}]"
            )
            if not isinstance(paragraph_raw, Mapping):
                raise ParameterAnalysisError(
                    f"{paragraph_prefix} must be an object"
                )
            allowed = {
                "analysis_kind",
                "basis_parameter_ids",
                "analysis_zh",
                "conditions_zh",
                "limitations_zh",
            }
            if set(paragraph_raw) - allowed:
                raise ParameterAnalysisError(
                    f"{paragraph_prefix} has unknown fields"
                )
            analysis_kind = paragraph_raw.get("analysis_kind")
            if analysis_kind not in ANALYSIS_KINDS:
                raise ParameterAnalysisError(
                    f"{paragraph_prefix}.analysis_kind is invalid"
                )
            basis_ids = paragraph_raw.get("basis_parameter_ids")
            minimum_basis = 0 if analysis_kind == "limitation" else 1
            if (
                not isinstance(basis_ids, list)
                or not minimum_basis <= len(basis_ids) <= 8
                or len(set(basis_ids)) != len(basis_ids)
                or any(item not in source_by_id for item in basis_ids)
            ):
                raise ParameterAnalysisError(
                    f"{paragraph_prefix}.basis_parameter_ids are invalid"
                )
            analysis_zh = _clean_text(
                paragraph_raw.get("analysis_zh"),
                f"{paragraph_prefix}.analysis_zh",
                required=True,
                limit=500,
                require_han=True,
            )
            if len(analysis_zh) < 40:
                raise ParameterAnalysisError(
                    f"{paragraph_prefix}.analysis_zh is too short"
                )
            conditions = _string_list(
                paragraph_raw.get("conditions_zh", []),
                f"{paragraph_prefix}.conditions_zh",
                limit=2,
                item_limit=300,
            )
            limitations = _string_list(
                paragraph_raw.get("limitations_zh", []),
                f"{paragraph_prefix}.limitations_zh",
                limit=2,
                item_limit=300,
            )
            basis_rows = [source_by_id[item] for item in basis_ids]
            _ground_numbers(
                [analysis_zh, *conditions, *limitations],
                basis_rows,
                paragraph_prefix,
            )
            paragraphs.append(
                {
                    "analysis_kind": analysis_kind,
                    "basis_parameter_ids": list(basis_ids),
                    "analysis_zh": analysis_zh,
                    "conditions_zh": conditions,
                    "limitations_zh": limitations,
                }
            )
        normalized_sections.append(
            {
                "section_code": section_code,
                "paragraphs": paragraphs,
            }
        )

    order = {code: index for index, code in enumerate(ANALYSIS_SECTION_TITLES)}
    normalized_sections.sort(key=lambda item: order[item["section_code"]])
    overall_limitations = _string_list(
        raw.get("overall_limitations_zh", []),
        "overall_limitations_zh",
        limit=5,
        item_limit=300,
    )
    if not overall_limitations:
        raise ParameterAnalysisError(
            "overall_limitations_zh must contain at least one limitation"
        )
    _ground_numbers(overall_limitations, [], "overall_limitations_zh")
    return {
        **expected_metadata,
        "translations": translations,
        "sections": normalized_sections,
        "overall_limitations_zh": overall_limitations,
    }


def apply_parameter_translations(
    parameters: Sequence[Mapping[str, Any]],
    enrichment: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Add reader-facing translations without changing grounded source fields."""

    normalized = validate_parameter_enrichment(enrichment, parameters)
    translated: list[dict[str, Any]] = []
    for item, translation in zip(
        parameters,
        normalized["translations"],
        strict=True,
    ):
        row = dict(item)
        for key in ("name_zh", "section_zh", "subsection_zh", "value_zh"):
            if translation[key]:
                row[key] = translation[key]
        translated.append(row)
    return translated


def professional_analysis(
    enrichment: Mapping[str, Any],
    parameters: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return the validated reader-facing analysis portion only."""

    normalized = validate_parameter_enrichment(enrichment, parameters)
    return {
        key: normalized[key]
        for key in (
            "schema_version",
            "prompt_version",
            "glossary_version",
            "input_parameter_count",
            "input_complete",
            "sections",
            "overall_limitations_zh",
        )
    }


__all__ = [
    "ANALYSIS_KINDS",
    "ANALYSIS_SECTION_TITLES",
    "MAX_ANALYSIS_INPUT_CHARS",
    "MAX_ANALYSIS_PARAMETERS",
    "PARAMETER_ANALYSIS_PROMPT_VERSION",
    "PARAMETER_ANALYSIS_SCHEMA_VERSION",
    "PARAMETER_GLOSSARY_VERSION",
    "ParameterAnalysisError",
    "SECTION_TRANSLATIONS",
    "apply_parameter_translations",
    "parameter_analysis_input",
    "parameter_id",
    "parameter_set_sha256",
    "professional_analysis",
    "validate_parameter_enrichment",
]
