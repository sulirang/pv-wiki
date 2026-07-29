"""Validate professional parameter translations and grounded AI analysis."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any


PARAMETER_ANALYSIS_SCHEMA_VERSION = 2
PARAMETER_ANALYSIS_PROMPT_VERSION = "pv-parameter-analysis-v14"
PARAMETER_GLOSSARY_VERSION = "pv-zh-technical-v5"
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
    (re.compile(r"\bMax(?:imum)?\.?\b", re.IGNORECASE), "最大"),
    (re.compile(r"\bMin(?:imum)?\.?\b", re.IGNORECASE), "最小"),
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
    (re.compile(r"\bAC\b", re.IGNORECASE), "交流"),
    (re.compile(r"\bDC\b", re.IGNORECASE), "直流"),
    (re.compile(r"\bPV\b", re.IGNORECASE), "光伏"),
)
_COMPOUND_REQUIRED_TERMS = (
    (
        re.compile(r"\bOver(?:[ -]?Current)\b", re.IGNORECASE),
        ("过流", "过电流"),
    ),
    (
        re.compile(r"\bOver(?:[ -]?voltage)\b", re.IGNORECASE),
        ("过压", "过电压"),
    ),
    (
        re.compile(r"\bCooling\s+Method\b", re.IGNORECASE),
        ("散热方式", "冷却方式"),
    ),
    (
        re.compile(r"^\s*Feed[- ]?in\s*$", re.IGNORECASE),
        ("并网接线方式", "并网接线制式", "馈电方式"),
    ),
    (
        re.compile(r"\bTopology\b", re.IGNORECASE),
        ("拓扑结构", "拓扑"),
    ),
    (
        re.compile(r"\bIngress\s+Protection\b", re.IGNORECASE),
        ("防护等级",),
    ),
)
_UNIT_ATOM_PATTERN = (
    r"(?:%|°[CF]|(?:p|n|u|µ|μ|m|c|d|k|M|G)?(?:"
    r"A(?:h|ac|dc)?|V(?:Ar|ac|dc|A)?|W(?:p|h)?|Hz|"
    r"Ω|ohm|g|m(?:2|3)?|s(?:2)?|h|Pa|bar|dB(?:A)?|rpm|[Yy]ears?"
    r")|K)"
)
_UNIT_ATOM_RE = re.compile(_UNIT_ATOM_PATTERN)
_PROTECTED_TOKEN_RE = re.compile(
    r"\[[^\[\]\r\n]{1,24}\]"
    rf"|(?<![A-Za-z0-9])\d+(?:\.\d+)?(?i:{_UNIT_ATOM_PATTERN})"
    r"(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])(?:"
    r"MPPT|THD[iI]?|DCI|GFCI|AFCI|STC|NMOT|"
    r"RS\d+|Wi-Fi|GPRS|[345]G|IP\d+"
    r")(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])\d+(?:\.\d+)?(?![A-Za-z0-9])"
)
_RESTORABLE_ABBREVIATION_RE = re.compile(
    r"(?:MPPT|THD[iI]?|DCI|GFCI|AFCI|STC|NMOT|RS\d+|"
    r"Wi-Fi|GPRS|[345]G|IP\d+)"
)
_BRACKETED_UNIT_CONTENT_RE = re.compile(
    rf"{_UNIT_ATOM_PATTERN}(?:\s*[·*/]\s*{_UNIT_ATOM_PATTERN})*",
    re.IGNORECASE,
)
_BRACKETED_TECHNICAL_CONTENT_RE = re.compile(
    r"(?:THD[iI]?|cos\s*[φΦ]|[HWD](?:\*[HWD]){1,2})"
)
_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])\d+(?:\.\d+)?")
_MAX_EXACT_DECIMAL_DIGITS = 256
_NUMERIC_TOKEN_PATTERN = (
    r"[+\-−–—＋－]?(?:"
    r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:[.,]\d+)?"
    r")"
)
_COMPARISON_PATTERN = (
    r"(?:不超过|不高于|不大于|不低于|不小于|至少|至多|"
    r"小于|低于|大于|高于|等于|<=|>=|≤|≥|<|>|±)"
)
_VALUE_SEPARATOR_PATTERN = r"(?:-|−|–|—|~|～|/|\*|×|x|X|至|到)"
_NUMERIC_TAIL_PATTERN = (
    rf"(?:\s*(?P<separator>{_VALUE_SEPARATOR_PATTERN})\s*"
    rf"(?P<second>{_NUMERIC_TOKEN_PATTERN})"
    rf"(?:\s*(?P<separator_3>{_VALUE_SEPARATOR_PATTERN})\s*"
    rf"(?P<third>{_NUMERIC_TOKEN_PATTERN}))?"
    rf"(?:\s*(?P<separator_4>{_VALUE_SEPARATOR_PATTERN})\s*"
    rf"(?P<fourth>{_NUMERIC_TOKEN_PATTERN}))?"
    r")?"
)
_EXPRESSION_START_GUARD = r"(?<![A-Za-z0-9.<>≤≥≦≧≠≈∓~∼+\-−–—＋－±/·⋅∙*×÷∕⁄])"
_UNIT_COMPONENT_PATTERN = (
    r"(?:%|Ω|°[A-Za-z]|"
    r"[A-Za-zμµ][A-Za-z0-9μµ]*(?:\([A-Za-z0-9]+\))?)"
)
_SYMBOL_UNIT_PATTERN = (
    rf"{_UNIT_COMPONENT_PATTERN}(?:\s*[·*/×]\s*"
    rf"{_UNIT_COMPONENT_PATTERN})*"
)
_CHINESE_UNIT_PATTERN = (
    r"(?:安培小时|瓦特小时|千瓦峰|兆瓦峰|千峰瓦|峰值瓦|兆瓦时|"
    r"千瓦时|毫安时|安时|瓦时|千伏安|兆伏安|伏安|兆瓦|千瓦|"
    r"瓦特|峰瓦|千伏|伏特|毫安|安培|赫兹|焦耳|摄氏度|华氏度|"
    r"立方米|平方米|毫米|厘米|千克|公斤|百分比|瓦|伏|安|米|年)"
)
_UNIT_TOKEN_PATTERN = rf"(?:{_SYMBOL_UNIT_PATTERN}|{_CHINESE_UNIT_PATTERN})"
_NUMERIC_EXPRESSION_RE = re.compile(
    rf"^\s*(?P<operator>{_COMPARISON_PATTERN})?\s*"
    rf"(?P<first>{_NUMERIC_TOKEN_PATTERN}){_NUMERIC_TAIL_PATTERN}\s*$"
)
_NUMERIC_EXPRESSION_SCAN_RE = re.compile(
    rf"{_EXPRESSION_START_GUARD}"
    rf"(?P<operator>{_COMPARISON_PATTERN})?\s*"
    rf"(?P<first>{_NUMERIC_TOKEN_PATTERN}){_NUMERIC_TAIL_PATTERN}"
)
_MEASUREMENT_CLAIM_RE = re.compile(
    rf"{_EXPRESSION_START_GUARD}"
    rf"(?P<operator>{_COMPARISON_PATTERN})?\s*"
    rf"(?P<first>{_NUMERIC_TOKEN_PATTERN}){_NUMERIC_TAIL_PATTERN}\s*"
    rf"(?P<unit>{_UNIT_TOKEN_PATTERN})(?=$|[为是的]|[^A-Za-z0-9μµ\u3400-\u9fff])"
)
_EXPLICIT_UNIT_CLAIM_RE = re.compile(
    rf"{_EXPRESSION_START_GUARD}"
    rf"(?P<operator>{_COMPARISON_PATTERN})?\s*"
    rf"(?P<first>{_NUMERIC_TOKEN_PATTERN}){_NUMERIC_TAIL_PATTERN}"
    r"\s*(?:，|,)?\s*(?:(?:其|所用|使用的?|采用的?|计量)?单位)"
    r"(?:是|为|：|:)\s*"
    rf"(?P<declared_unit>{_UNIT_TOKEN_PATTERN}|[\u3400-\u9fff]{{1,12}})"
)
_FALLBACK_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|"
    r"\d+(?:[.,]\d+)?)"
)
_SEMANTIC_POSTFIX_BASE = (
    r"(?:左右|上下|以上|以下|以内|以外|附近|更高|更低|更多|更少)"
)
_PUNCTUATED_SEMANTIC_POSTFIX_BASE = (
    r"(?:左右|以上|以下|以内|以外|附近|更高|更低|更多|更少)"
)
_SEMANTIC_POSTFIX_PATTERN = (
    rf"(?:级\s*)?(?:(?:及|且|或)\s*)?{_SEMANTIC_POSTFIX_BASE}"
)
_PUNCTUATED_SEMANTIC_POSTFIX_PATTERN = (
    rf"(?:级\s*)?(?:(?:及|且|或)\s*)?"
    rf"{_PUNCTUATED_SEMANTIC_POSTFIX_BASE}"
)
_CLAIM_HAN_POSTFIX_PATTERN = (
    r"(?:(?:的\s*)?(?:(?:功率|容量)?(?:等级|级别)|级|档|规格(?:产品|机型|型号|设备)?|"
    r"系列|款(?:逆变器|机型|型号|产品|设备)?|版本|类别|类型|类(?:产品|机型|型号|设备|逆变器)|"
    r"对应(?:机型|型号|产品|设备|逆变器)|机型|型号|产品|设备|逆变器)|"
    r"(?:的\s*)?(?:近似值|估计值|估算值|量级|范围|上限|下限|左右|上下|"
    r"以上|以下|以内|以外|附近))"
)
_CLAIM_SUFFIX_BLOCK_RE = re.compile(
    rf"^\s*(?:{_SEMANTIC_POSTFIX_PATTERN}|"
    rf"[，,、；;。.!！？?]\s*{_PUNCTUATED_SEMANTIC_POSTFIX_PATTERN}|"
    rf"{_CLAIM_HAN_POSTFIX_PATTERN}|每|"
    r"[+\-−–—＋－±∓<>≤≥≦≧≠≈~∼/·⋅∙*×÷∕⁄]|[²³]|[A-Za-zμµΩ°]|"
    r"\((?!(?:STC|NMOT)\s*\))|[（\[【\)）\]】])",
    re.IGNORECASE,
)
_CLAIM_PREFIX_BLOCK_RE = re.compile(
    r"(?:[+\-−–—＋－±∓<>≤≥≦≧≠≈~∼/·⋅∙*×÷∕⁄()\[\]（）【】]|"
    r"负|正|约|大约|接近|近似|估计|预计)\s*$"
)
_RAW_CLAIM_SUFFIX_BLOCK_RE = re.compile(
    r"^\s*(?:[\[(]|[/·⋅∙*×÷∕⁄]|[²³]|[A-Za-zμµΩ°%]|"
    r"[\u3400-\u9fff])"
)
_HAN_NUMBER_PATTERN = (
    r"[零〇○一二两兩三四五六七八九十百千万萬亿億"
    r"壹贰貳叁參肆伍陆陸柒捌玖拾佰仟单双單雙俩倆半]+"
)
_HAN_NUMERIC_UNIT_RE = re.compile(
    rf"{_HAN_NUMBER_PATTERN}\s*(?:{_UNIT_TOKEN_PATTERN})"
)
_HAN_COUNT_CLAIM_RE = re.compile(
    rf"(?:{_HAN_NUMBER_PATTERN}\s*(?:(?:个|路|组|项|套|台)\s*)?MPPT|"
    rf"(?:MPPT\s*)?(?:数量|数目|个数|路数)\s*(?:为|是|：|:)\s*"
    rf"{_HAN_NUMBER_PATTERN})",
    re.IGNORECASE,
)
_HAN_CLASSIFIED_COUNT_RE = re.compile(
    rf"(?:{_HAN_NUMBER_PATTERN}\s*(?:路|组|套|台|相)|"
    rf"{_HAN_NUMBER_PATTERN}\s*(?:个|项)\s*(?:MPPT|直流|交流|光伏|"
    r"电池|组件|组串|输入|输出|接口|端口|回路|支路|通道|模块|"
    r"设备|逆变器|保护))",
    re.IGNORECASE,
)
_HAN_ENGINEERING_TOPOLOGY_RE = re.compile(
    rf"{_HAN_NUMBER_PATTERN}\s*(?:根\s*(?:相线|导线|线)|"
    r"(?:相)?线制|电平(?:拓扑|结构)?|相制)",
    re.IGNORECASE,
)
_HAN_DERIVED_METRIC_RE = re.compile(
    rf"(?:百分之{_HAN_NUMBER_PATTERN}|"
    rf"(?:{_HAN_NUMBER_PATTERN}又)?{_HAN_NUMBER_PATTERN}分之"
    rf"{_HAN_NUMBER_PATTERN}|{_HAN_NUMBER_PATTERN}[点點]"
    rf"{_HAN_NUMBER_PATTERN}|{_HAN_NUMBER_PATTERN}\s*"
    rf"(?:倍|成|折|比(?:例|值)?)|"
    rf"{_HAN_NUMBER_PATTERN}\s*个百分点|"
    rf"{_HAN_NUMBER_PATTERN}\s*[∶:：]\s*{_HAN_NUMBER_PATTERN})",
    re.IGNORECASE,
)
_HAN_IMPLICIT_DERIVED_METRIC_RE = re.compile(
    r"(?:(?:一|壹)半(?!导体|導體)|半数|半數|减半|減半|折半|"
    r"(?<!针)(?<!針)对半(?!导体|導體)|對半(?!導體)|"
    r"翻\s*(?:了\s*)?(?:(?:一|壹|二|两|兩|貳)\s*)?番(?!茄)|翻倍|"
    r"(?:数|數|几|幾|若干)倍|倍(?:減|减)|"
    r"(?:能力|功率|容量|数值|數值|规模|規模|输出|輸出|输入|輸入|"
    r"电压|電壓|电流|電流|效率|数量|數量|幅度|水平|结果|結果)"
    r"\s*(?:已经|已經|已|将|將|会|會|可|能够|能夠)?\s*加倍|"
    r"(?:呈|为|為|达到|達到|实现|實現|形成|发生|發生)\s*"
    r"(?:加倍|倍增|成倍)(?:趋势|趨勢|关系|關係|结果|結果|幅度|"
    r"水平|增长|增長|增加|提升|扩大|擴大|下降|变化|變化|增幅|降幅)?|"
    r"(?:加倍|倍增|成倍)\s*(?:趋势|趨勢|关系|關係|结果|結果|"
    r"幅度|水平|增长|增長|增加|提升|扩大|擴大|下降|变化|變化|"
    r"增幅|降幅))",
    re.IGNORECASE,
)
_HAN_COUNT_CIRCUMLOCUTION_RE = re.compile(
    rf"(?:{_HAN_NUMBER_PATTERN}\s*(?:MPPT|通道|回路|支路|线路|線路|"
    r"端口|接口|模块|模組|组串|組串|跟踪器|追踪器|追蹤器|"
    r"跟踪通道|追踪通道|追蹤通道|跟踪回路|追踪回路|追蹤回路)|"
    rf"{_HAN_NUMBER_PATTERN}\s*(?:个|個|项|項|路|组|組|套|台|相|"
    r"对|對|只|条|條)\s*[\u3400-\u9fff]{0,24}"
    r"(?:MPPT|通道|回路|支路|线路|線路|端口|接口|模块|模組|"
    r"组串|組串|跟踪器|追踪器|追蹤器|跟踪通道|追踪通道|"
    r"追蹤通道|跟踪回路|追踪回路|追蹤回路))",
    re.IGNORECASE,
)
_DERIVED_ARITHMETIC_RESULT_RE = re.compile(
    r"(?:总和|總和|合计|合計|加总|加總|之和|差值|之差|乘积|乘積|"
    r"之积|之積|商值|之商|比值|比率|比例|倍数|倍數|容配比|利用率)"
    r"[\s，,：:]{0,12}(?:为|為|是(?!否|不)|就是|即|即为|即為|恰为|恰為|"
    r"等于|等於|达到|達到)|"
    r"(?:相加|加上|相减|相減|减去|減去|相乘|乘以|相除|除以|"
    r"加总|加總|计算|計算)[^；;。！？!?]{0,80}?"
    r"(?:为|為|是(?!否|不)|就是|即|即为|即為|恰为|恰為|等于|等於|"
    r"得到|得出|可得|计算得|計算得|推导得|推導得|获得|獲得)|"
    r"(?:等于|等於|相当于|相當於|就是|即|即为|即為|恰为|恰為)"
    r"[^；;。！？!?]{0,80}?(?:总和|總和|之和|差值|之差|乘积|乘積|"
    r"之积|之積|商值|之商|比值|比率|比例|倍数|倍數|容配比|利用率)|"
    r"\b(?:sum|difference|product|quotient|ratio|multiple)\s+"
    r"(?:is|equals?)\b|\b(?:calculated|derived)\s+(?:as|to be)\b",
    re.IGNORECASE,
)
_MULTI_ROW_REFERENCE_RE = re.compile(
    r"(?:两者|兩者|二者|双方|雙方|前者.{0,24}后者|前者.{0,24}後者|"
    r"上述两项|上述兩項|这两项|這兩項|两个参数|兩個參數)"
)
_ENGLISH_NUMBER_WORD_PATTERN = (
    r"(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|"
    r"twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|"
    r"nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|"
    r"hundred|half)"
)
_ENGLISH_DERIVED_METRIC_RE = re.compile(
    rf"(?<![A-Za-z])(?:{_ENGLISH_NUMBER_WORD_PATTERN}\s+point\s+"
    rf"{_ENGLISH_NUMBER_WORD_PATTERN}(?:\s+(?:times?|fold|percent))?|"
    rf"{_ENGLISH_NUMBER_WORD_PATTERN}(?:\s+and\s+(?:a\s+)?half)?"
    rf"\s+(?:times?|fold|percent)|twice|one[-\s]+half|"
    r"(?:has|have|had|is|was|were)\s+doubled|"
    r"(?:is|equals?|becomes?|became)\s+double"
    r"(?!\s+(?:insulation|isolation|pole|stage|layer|winding))|"
    rf"{_ENGLISH_NUMBER_WORD_PATTERN}(?:[-\s]+"
    rf"{_ENGLISH_NUMBER_WORD_PATTERN})?\s+percent)(?![A-Za-z])",
    re.IGNORECASE,
)
_HAN_BRACKETED_UNIT_RE = re.compile(
    rf"{_HAN_NUMBER_PATTERN}\s*[\[(（]\s*(?:{_UNIT_TOKEN_PATTERN})"
)
_COUNT_SOURCE_NAME_RE = re.compile(r"\b(?:Number|Count|Quantity)\b", re.IGNORECASE)
_COUNT_ASSIGNMENT_PATTERN = (
    r"(?:为|為|是|共(?:有|计|計)?|有|设有|設有|配有|配备|配備|"
    r"配置(?:为|為|成|了|有)?|采用|採用|体现为|體現為|写成|寫成|"
    r"改写成|改寫成|达到|達到)"
)
_COUNT_CLASSIFIER_RE = re.compile(
    r"^\s*(?P<classifier>个|路|组|项|套)(?![\u3400-\u9fff])"
)
_NUMERIC_TECHNICAL_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:IP\d+|RS\d+|RJ\d+|MC\d+|[345]G|\d+L)"
    r"(?![A-Za-z0-9])"
)
_COMPOSITE_TOPOLOGY_VALUE_RE = re.compile(
    r"^\s*\d+L(?:\s*[+/]\s*(?:N|PE))+\s*$",
    re.IGNORECASE,
)
_CODE_ONLY_ALPHA_VALUES = frozenset({"AFD"})
_TECHNICAL_TOKEN_SHARE_GAP_RE = re.compile(
    r"^\s*(?:(?:[，,、/+]|和|及|与|以及)\s*)$"
)
_ANY_DIGIT_RE = re.compile(r"\d+(?:[.,]\d+)?")
_UNSUPPORTED_TRANSLATED_HAN_NUMBER_RE = re.compile(
    r"[壹贰叁肆伍陆柒捌玖拾佰仟萬]+|"
    r"(?<![\u3400-\u9fff])半(?![\u3400-\u9fff])"
)
_HAN_NUMBER_TOKEN_RE = re.compile(_HAN_NUMBER_PATTERN)
_SIMPLE_HAN_NUMBER_VALUES = {
    "零": Fraction(0),
    "〇": Fraction(0),
    "一": Fraction(1),
    "单": Fraction(1),
    "二": Fraction(2),
    "两": Fraction(2),
    "俩": Fraction(2),
    "双": Fraction(2),
    "三": Fraction(3),
    "四": Fraction(4),
    "五": Fraction(5),
    "六": Fraction(6),
    "七": Fraction(7),
    "八": Fraction(8),
    "九": Fraction(9),
    "十": Fraction(10),
    "半": Fraction(1, 2),
}
_ENGLISH_NUMBER_VALUES = {
    "zero": Fraction(0),
    "one": Fraction(1),
    "single": Fraction(1),
    "two": Fraction(2),
    "double": Fraction(2),
    "dual": Fraction(2),
    "three": Fraction(3),
    "four": Fraction(4),
    "five": Fraction(5),
    "six": Fraction(6),
    "seven": Fraction(7),
    "eight": Fraction(8),
    "nine": Fraction(9),
    "ten": Fraction(10),
    "half": Fraction(1, 2),
}
_DEFAULT_IGNORABLE_RANGES = (
    (0x034F, 0x034F),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFFA0, 0xFFA0),
    (0xE0100, 0xE01EF),
)
_LABEL_ANNOTATION_CANDIDATE_RE = re.compile(
    r"\s*(?:\[[^\[\]\r\n]{1,24}\]|\([^()\r\n]{1,24}\)|"
    rf"@\s*(?:STC|NMOT|\d+(?:\.\d+)?{_UNIT_ATOM_PATTERN}))",
    re.IGNORECASE,
)
_POWER_UNIT_CANONICAL = {
    "w": "W",
    "kw": "kW",
    "wp": "Wp",
    "kwp": "kWp",
}
_POWER_UNIT_FACTORS = {
    "W": ("W", 1),
    "kW": ("W", 1000),
    "Wp": ("Wp", 1),
    "kWp": ("Wp", 1000),
}
_CHINESE_UNIT_ALIASES = {
    "千瓦": "kW",
    "瓦特": "W",
    "千瓦峰": "kWp",
    "千峰瓦": "kWp",
    "峰值瓦": "Wp",
    "峰瓦": "Wp",
    "兆瓦峰": "MWp",
    "兆瓦": "MW",
    "瓦": "W",
    "伏": "V",
    "伏特": "V",
    "千伏": "kV",
    "安": "A",
    "安培": "A",
    "安培小时": "Ah",
    "安时": "Ah",
    "毫安": "mA",
    "毫安时": "mAh",
    "伏安": "VA",
    "千伏安": "kVA",
    "兆伏安": "MVA",
    "赫兹": "Hz",
    "焦耳": "J",
    "摄氏度": "°C",
    "华氏度": "°F",
    "米": "m",
    "毫米": "mm",
    "厘米": "cm",
    "平方米": "m2",
    "立方米": "m3",
    "千克": "kg",
    "公斤": "kg",
    "年": "year",
    "百分比": "%",
    "瓦时": "Wh",
    "瓦特小时": "Wh",
    "千瓦时": "kWh",
    "兆瓦时": "MWh",
}
_HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_MARKDOWN_OR_HTML_RE = re.compile(
    r"(?:^|\n)\s{0,3}(?:#{1,6}|[-*+]\s|>\s|```)|<[^>\r\n]+>",
    re.MULTILINE,
)


class ParameterAnalysisError(ValueError):
    """Raised when translations or analysis cannot be safely published."""


@dataclass(frozen=True, slots=True)
class _NumericExpression:
    """Canonical numeric meaning independent of locale punctuation."""

    comparator: str
    relation: str
    values: tuple[Fraction, ...]
    signs: tuple[str, ...] = ()


def parameter_id(index: int) -> str:
    """Return the stable positional ID used only within one parameter set."""

    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise ValueError("parameter index must be a non-negative integer")
    return f"p{index + 1:03d}"

def _is_default_ignorable(character: str) -> bool:
    codepoint = ord(character)
    category = unicodedata.category(character)
    return category == "Cf" or category.startswith("M") or any(
        start <= codepoint <= end
        for start, end in _DEFAULT_IGNORABLE_RANGES
    )


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
    cleaned = unicodedata.normalize(
        "NFC",
        " ".join(value.replace("\x00", "").split()),
    )
    if any(_is_default_ignorable(character) for character in cleaned):
        raise ParameterAnalysisError(
            f"{field} cannot contain invisible Unicode format characters"
        )
    if required and not cleaned:
        raise ParameterAnalysisError(f"{field} is required")
    if len(cleaned) > limit:
        raise ParameterAnalysisError(f"{field} exceeds {limit} characters")
    if cleaned and require_han and _HAN_RE.search(cleaned) is None:
        raise ParameterAnalysisError(f"{field} must contain professional Chinese")
    if cleaned and _MARKDOWN_OR_HTML_RE.search(cleaned):
        raise ParameterAnalysisError(f"{field} cannot contain Markdown or HTML")
    return cleaned


def _identifier_only_heading(value: str) -> bool:
    """Whether a heading is a model/code label with no prose to translate."""

    normalized = unicodedata.normalize("NFKC", value).strip()
    return (
        bool(re.search(r"\d", normalized))
        and re.fullmatch(r"[A-Za-z0-9_.+/@() /\-]+", normalized) is not None
        and re.search(
            r"(?<![A-Za-z0-9])[A-Za-z]{3,}(?![A-Za-z0-9])",
            normalized,
        )
        is None
    )


def _code_only_source_value(value: str) -> bool:
    """Whether a source value is one standalone, non-translatable code."""

    normalized = unicodedata.normalize("NFKC", value).strip().upper()
    return (
        normalized in _CODE_ONLY_ALPHA_VALUES
        or _NUMERIC_TECHNICAL_TOKEN_RE.fullmatch(normalized) is not None
    )


def _heading_translation(
    raw_value: Any,
    source_value: str,
    field: str,
) -> str:
    identifier_only = _identifier_only_heading(source_value)
    translated = _clean_text(
        raw_value,
        field,
        required=bool(source_value) and not identifier_only,
        limit=100,
    )
    if identifier_only:
        return ""
    if translated and _HAN_RE.search(translated) is None:
        raise ParameterAnalysisError(
            f"{field} must contain professional Chinese"
        )
    return translated


def _restore_name_tokens(
    name_zh: str,
    source_name: str,
    prefix: str,
) -> str:
    """Append exact source abbreviations/units without inventing translation."""

    def contains_exact_token(text: str, token: str) -> bool:
        normalized_text = unicodedata.normalize("NFKC", text)
        normalized_token = unicodedata.normalize("NFKC", token)
        if normalized_token.startswith("[") and normalized_token.endswith("]"):
            return normalized_token in normalized_text
        if _NUMBER_RE.fullmatch(normalized_token):
            return normalized_token in _NUMBER_RE.findall(normalized_text)
        return (
            re.search(
                rf"(?<![A-Za-z0-9]){re.escape(normalized_token)}"
                r"(?![A-Za-z0-9])",
                normalized_text,
            )
            is not None
        )

    def is_bracketed_unit(token: str) -> bool:
        if not token.startswith("[") or not token.endswith("]"):
            return False
        content = unicodedata.normalize("NFKC", token[1:-1]).strip()
        return (
            _BRACKETED_UNIT_CONTENT_RE.fullmatch(content) is not None
            or _BRACKETED_TECHNICAL_CONTENT_RE.fullmatch(content) is not None
        )

    restored = name_zh
    for token in dict.fromkeys(_PROTECTED_TOKEN_RE.findall(source_name)):
        if contains_exact_token(restored, token):
            continue
        if is_bracketed_unit(token):
            suffix = f" {token}"
        elif _RESTORABLE_ABBREVIATION_RE.fullmatch(token):
            suffix = f"（{token}）"
        else:
            raise ParameterAnalysisError(
                f"{prefix}.name_zh must preserve protected token {token}"
            )
        if len(restored) + len(suffix) > 200:
            raise ParameterAnalysisError(f"{prefix}.name_zh exceeds 200 characters")
        restored += suffix
    return restored


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


def _source_simple_numeric_values(source: str) -> set[Fraction]:
    values: set[Fraction] = set()
    normalized = unicodedata.normalize("NFKC", source)
    for match in _FALLBACK_NUMBER_RE.finditer(normalized):
        try:
            values.add(Fraction(match.group().replace(",", "")))
        except (ValueError, ZeroDivisionError):
            continue
    for word in re.findall(r"[A-Za-z]+", normalized.casefold()):
        value = _ENGLISH_NUMBER_VALUES.get(word)
        if value is not None:
            values.add(value)
    for match in _HAN_NUMBER_TOKEN_RE.finditer(normalized):
        value = _SIMPLE_HAN_NUMBER_VALUES.get(match.group())
        if value is not None:
            values.add(value)
    return values


def _reject_added_han_numeric_claims(
    translated: str,
    source: str,
    field: str,
) -> None:
    source_values = _source_simple_numeric_values(source)
    for pattern in (
        _HAN_NUMERIC_UNIT_RE,
        _HAN_BRACKETED_UNIT_RE,
        _HAN_CLASSIFIED_COUNT_RE,
        _HAN_ENGINEERING_TOPOLOGY_RE,
        _HAN_DERIVED_METRIC_RE,
        _HAN_COUNT_CIRCUMLOCUTION_RE,
        _HAN_COUNT_CLAIM_RE,
    ):
        for match in pattern.finditer(translated):
            number_match = _HAN_NUMBER_TOKEN_RE.search(match.group())
            value = (
                _SIMPLE_HAN_NUMBER_VALUES.get(number_match.group())
                if number_match is not None
                else None
            )
            if value is None or value not in source_values:
                raise ParameterAnalysisError(
                    f"{field} introduces unsupported Chinese numeric text"
                )


def _reject_added_numeric_tokens(
    translated: str,
    source: str,
    field: str,
) -> None:
    """Preserve numeric and technical tokens in source order and multiplicity."""

    normalized_source = unicodedata.normalize("NFKC", source)
    normalized_translated = unicodedata.normalize("NFKC", translated)
    source_tokens = _ANY_DIGIT_RE.findall(normalized_source)
    _reject_added_han_numeric_claims(
        normalized_translated,
        normalized_source,
        field,
    )
    translated_tokens = _ANY_DIGIT_RE.findall(normalized_translated)
    added = [
        token
        for token in translated_tokens
        if token not in set(source_tokens)
    ]
    if added:
        raise ParameterAnalysisError(
            f"{field} introduces numeric tokens absent from its source: "
            f"{', '.join(list(dict.fromkeys(added))[:8])}"
        )
    if translated and translated_tokens != source_tokens:
        raise ParameterAnalysisError(
            f"{field} must preserve numeric tokens in source order and multiplicity"
        )

    source_technical_tokens = _NUMERIC_TECHNICAL_TOKEN_RE.findall(
        normalized_source
    )
    translated_technical_tokens = _NUMERIC_TECHNICAL_TOKEN_RE.findall(
        normalized_translated
    )
    added_technical_tokens = [
        token
        for token in translated_technical_tokens
        if token not in set(source_technical_tokens)
    ]
    if added_technical_tokens:
        raise ParameterAnalysisError(
            f"{field} introduces numeric technical tokens absent from its source: "
            f"{', '.join(list(dict.fromkeys(added_technical_tokens))[:8])}"
        )
    if translated and translated_technical_tokens != source_technical_tokens:
        raise ParameterAnalysisError(
            f"{field} must preserve numeric technical tokens in source order"
        )

    source_han_numbers = _UNSUPPORTED_TRANSLATED_HAN_NUMBER_RE.findall(
        normalized_source
    )
    translated_han_numbers = _UNSUPPORTED_TRANSLATED_HAN_NUMBER_RE.findall(
        normalized_translated
    )
    added_han_numbers = [
        token for token in translated_han_numbers if token not in source_han_numbers
    ]
    if added_han_numbers:
        raise ParameterAnalysisError(
            f"{field} introduces unsupported Chinese numeric text"
        )


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
    generic_source_name = source_name
    for compound_pattern, accepted_terms in _COMPOUND_REQUIRED_TERMS:
        if not compound_pattern.search(generic_source_name):
            continue
        if not any(term in name_zh for term in accepted_terms):
            raise ParameterAnalysisError(
                f"{prefix}.name_zh must preserve the controlled compound term "
                f"{' or '.join(accepted_terms)}"
            )
        generic_source_name = compound_pattern.sub(" ", generic_source_name)
    for pattern, required_term in _REQUIRED_TERMS:
        if not pattern.search(generic_source_name):
            continue
        if required_term not in name_zh:
            raise ParameterAnalysisError(
                f"{prefix}.name_zh must preserve the controlled term "
                f"{required_term}"
            )
    name_zh = _restore_name_tokens(name_zh, source_name, prefix)
    _reject_added_numeric_tokens(name_zh, source_name, f"{prefix}.name_zh")

    source_section = str(source["section"])
    section_zh = _heading_translation(
        raw.get("section_zh", ""),
        source_section,
        f"{prefix}.section_zh",
    )
    _reject_added_numeric_tokens(
        section_zh,
        source_section,
        f"{prefix}.section_zh",
    )
    expected_section = SECTION_TRANSLATIONS.get(source_section.casefold())
    if expected_section is not None and section_zh != expected_section:
        raise ParameterAnalysisError(
            f"{prefix}.section_zh must use the controlled section translation"
        )
    for token in _PROTECTED_TOKEN_RE.findall(source_section):
        if section_zh and token not in section_zh:
            raise ParameterAnalysisError(
                f"{prefix}.section_zh must preserve protected token {token}"
            )

    source_subsection = str(source["subsection"])
    subsection_zh = _heading_translation(
        raw.get("subsection_zh", ""),
        source_subsection,
        f"{prefix}.subsection_zh",
    )
    _reject_added_numeric_tokens(
        subsection_zh,
        source_subsection,
        f"{prefix}.subsection_zh",
    )
    expected_subsection = SECTION_TRANSLATIONS.get(source_subsection.casefold())
    if expected_subsection is not None and subsection_zh != expected_subsection:
        raise ParameterAnalysisError(
            f"{prefix}.subsection_zh must use the controlled section "
            "translation"
        )
    for token in _PROTECTED_TOKEN_RE.findall(source_subsection):
        if subsection_zh and token not in subsection_zh:
            raise ParameterAnalysisError(
                f"{prefix}.subsection_zh must preserve protected token {token}"
            )

    source_value = str(source["value"])
    value_zh = _clean_text(
        raw.get("value_zh", ""),
        f"{prefix}.value_zh",
        limit=200,
    )
    if value_zh and _COMPOSITE_TOPOLOGY_VALUE_RE.fullmatch(source_value):
        raise ParameterAnalysisError(
            f"{prefix}.value_zh must be empty for a composite topology code"
        )
    if value_zh and _code_only_source_value(source_value):
        raise ParameterAnalysisError(
            f"{prefix}.value_zh must be empty for a code-only source value"
        )
    if value_zh and _HAN_RE.search(value_zh) is None:
        raise ParameterAnalysisError(
            f"{prefix}.value_zh must contain professional Chinese"
        )
    _reject_added_numeric_tokens(value_zh, source_value, f"{prefix}.value_zh")
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


def _canonical_comparator(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    return {
        "小于": "<",
        "低于": "<",
        "<": "<",
        "不超过": "≤",
        "不高于": "≤",
        "不大于": "≤",
        "至多": "≤",
        "<=": "≤",
        "≤": "≤",
        "大于": ">",
        "高于": ">",
        ">": ">",
        "不低于": "≥",
        "不小于": "≥",
        "至少": "≥",
        ">=": "≥",
        "≥": "≥",
        "等于": "",
        "±": "±",
        "": "",
    }.get(normalized, normalized)


def _exact_decimal_fraction(value: str) -> Fraction | None:
    """Convert one unsigned decimal without context rounding or huge ints."""

    whole, separator, fractional = value.partition(".")
    digits = f"{whole}{fractional}" if separator else whole
    if not digits or len(digits) > _MAX_EXACT_DECIMAL_DIGITS:
        return None
    return Fraction(int(digits), 10 ** len(fractional))


def _parse_number_token(value: str) -> tuple[Fraction, str] | None:
    normalized = unicodedata.normalize("NFKC", value).strip()
    sign_multiplier = 1
    sign_marker = ""
    if normalized[:1] in {"-", "−", "–", "—"}:
        sign_multiplier = -1
        sign_marker = "-"
        normalized = normalized[1:]
    elif normalized[:1] == "+":
        sign_marker = "+"
        normalized = normalized[1:]
    if not normalized:
        return None

    if "," in normalized and "." in normalized:
        if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", normalized) is None:
            return None
        normalized = normalized.replace(",", "")
    elif "," in normalized:
        groups = normalized.split(",")
        grouped = (
            groups[0] != "0"
            and 1 <= len(groups[0]) <= 3
            and len(groups) > 1
            and all(len(group) == 3 for group in groups[1:])
        )
        if grouped:
            normalized = "".join(groups)
        elif len(groups) == 2 and all(groups):
            normalized = ".".join(groups)
        else:
            return None
    number = _exact_decimal_fraction(normalized)
    if number is None:
        return None
    return sign_multiplier * number, sign_marker


def _numeric_expression(match: re.Match[str]) -> _NumericExpression | None:
    group_values = match.groupdict()
    raw_numbers = [
        group_values.get(name)
        for name in ("first", "second", "third", "fourth")
    ]
    parsed_numbers = [
        _parse_number_token(value)
        for value in raw_numbers
        if value is not None
    ]
    if not parsed_numbers or any(item is None for item in parsed_numbers):
        return None
    values = tuple(item[0] for item in parsed_numbers if item is not None)
    signs = tuple(item[1] for item in parsed_numbers if item is not None)

    raw_separators = [
        group_values.get(name)
        for name in ("separator", "separator_3", "separator_4")
        if group_values.get(name) is not None
    ]
    relations: set[str] = set()
    for raw_separator in raw_separators:
        separator = unicodedata.normalize("NFKC", raw_separator).strip()
        if separator == "/":
            relations.add("choice")
        elif separator in {"*", "×", "x", "X"}:
            relations.add("product")
        else:
            relations.add("range")
    if len(relations) > 1:
        return None
    relation = next(iter(relations), "")
    if relation == "range" and len(values) != 2:
        return None
    return _NumericExpression(
        comparator=_canonical_comparator(match.group("operator") or ""),
        relation=relation,
        values=values,
        signs=signs,
    )



def parameter_numeric_guidance(parameter: Mapping[str, Any]) -> dict[str, Any]:
    """Describe which numeric forms one row may safely contribute to analysis."""

    source_name = unicodedata.normalize(
        "NFKC",
        str(parameter.get("name", "")),
    )
    source_value = unicodedata.normalize(
        "NFKC",
        str(parameter.get("value", "")),
    ).strip()
    source_unit = str(parameter.get("unit", "")).strip()
    value_match = _NUMERIC_EXPRESSION_RE.fullmatch(source_value)
    expression = _numeric_expression(value_match) if value_match else None
    technical_tokens = list(
        dict.fromkeys(
            match.group()
            for match in _NUMERIC_TECHNICAL_TOKEN_RE.finditer(
                f"{source_name} {source_value}"
            )
        )
    )
    if expression is not None and source_unit:
        mode = "complete_measurement"
    elif expression is not None and _COUNT_SOURCE_NAME_RE.search(source_name):
        mode = "complete_count_expression"
    elif expression is not None:
        mode = "complete_unitless_expression"
    elif _COMPOSITE_TOPOLOGY_VALUE_RE.fullmatch(source_value):
        mode = "no_numeric_restatement"
        technical_tokens = []
    elif technical_tokens:
        mode = "exact_technical_tokens_only"
    else:
        mode = "no_numeric_restatement"
    return {
        "mode": mode,
        "allowed_numeric_technical_tokens": technical_tokens,
    }


def _source_expression_variants(
    expression: _NumericExpression,
    source_name: str,
) -> set[_NumericExpression]:
    variants = {expression}
    if expression.comparator or expression.relation or len(expression.values) != 1:
        return variants
    if re.search(r"\bMax(?:imum)?\.?\b", source_name, re.IGNORECASE):
        variants.add(
            _NumericExpression("≤", "", expression.values, expression.signs)
        )
    if re.search(r"\bMin(?:imum)?\.?\b", source_name, re.IGNORECASE):
        variants.add(
            _NumericExpression("≥", "", expression.values, expression.signs)
        )
    return variants


def _canonical_unit(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().replace("µ", "μ")
    normalized = re.sub(r"\s*([·*/×])\s*", r"\1", normalized)
    normalized = re.sub(
        r"\((?:STC|NMOT)\)$",
        "",
        normalized,
        flags=re.IGNORECASE,
    ).strip()
    alias = _CHINESE_UNIT_ALIASES.get(normalized)
    if alias is not None:
        return alias
    power_unit = _POWER_UNIT_CANONICAL.get(normalized.casefold())
    if power_unit is not None:
        return power_unit
    if normalized.casefold() in {"year", "years"}:
        return "year"
    return normalized


def _normalized_analysis_label(value: str) -> str:
    """Strip only recognized unit/condition annotations from a Chinese label."""

    normalized = unicodedata.normalize("NFKC", value)

    def safe_annotation(match: re.Match[str]) -> str:
        token = match.group().strip()
        if token.casefold().startswith("@"):
            return ""
        if token.startswith("[") and token.endswith("]"):
            content = token[1:-1].strip()
            if (
                _BRACKETED_UNIT_CONTENT_RE.fullmatch(content)
                or _BRACKETED_TECHNICAL_CONTENT_RE.fullmatch(content)
            ):
                return ""
        if token.startswith("(") and token.endswith(")"):
            if token[1:-1].strip().casefold() in {"stc", "nmot"}:
                return ""
        return match.group()

    without_safe_annotations = _LABEL_ANNOTATION_CANDIDATE_RE.sub(
        safe_annotation,
        normalized,
    )
    return " ".join(without_safe_annotations.split())


def _label_is_locally_bound(
    text: str,
    allowed_labels: Sequence[str],
    claim_start: int,
    *,
    all_labels: Sequence[str],
) -> bool:
    """Bind a claim to the nearest complete semantic parameter label."""

    prefix = _normalized_analysis_label(text[:claim_start])
    blocked_gap = re.compile(
        r"[，,。；;！？!?]|\d|但|然而|不|非|无|未|约|接近|近似|"
        r"大概|估计|预计|可能|或许|左右|以上|以下|最多|超过|不到|"
        r"不少于|不多于|每|单位面积|最大|最小|额定|标称|直流|交流|"
        r"光伏|功率|电压|电流|范围"
    )
    blocked_label_prefix = re.compile(
        r"(?:非|无|未|不是|并非|并不|最大|最小|额定|标称|直流|交流|"
        r"光伏|单位面积|备用|合计|峰值|总(?:计|体)?|"
        r"(?:(?:每|各)(?:一)?(?:个|路|相|组|台|套|项|机)?|"
        r"单(?:台|机|路|相|组)))(?:的)?$"
    )
    candidates: list[tuple[int, int, str]] = []
    for label in dict.fromkeys(all_labels):
        if not label:
            continue
        position = prefix.rfind(label)
        if position < 0:
            continue
        gap = prefix[position + len(label):]
        if len(gap) > 48 or blocked_gap.search(gap):
            continue
        if blocked_label_prefix.search(prefix[:position]):
            continue
        candidates.append((position + len(label), len(label), label))
    if not candidates:
        return False
    bound_label = max(candidates)[2]
    return bound_label in set(allowed_labels)


def _basis_measurement_evidence(
    basis_rows: Sequence[Mapping[str, Any]],
    basis_labels: Sequence[str],
) -> tuple[
    dict[tuple[_NumericExpression, str], set[str]],
    dict[tuple[str, Fraction, str, tuple[str, ...]], set[tuple[str, str]]],
    dict[tuple[_NumericExpression, str], set[str]],
    dict[_NumericExpression, set[str]],
    dict[_NumericExpression, set[str]],
    dict[str, set[tuple[int, str]]],
    dict[int, tuple[str, ...]],
]:
    """Collect expression-, unit-, label-, and technical-token evidence."""

    exact_measurements: dict[
        tuple[_NumericExpression, str],
        set[str],
    ] = {}
    power_conversions: dict[
        tuple[str, Fraction, str, tuple[str, ...]],
        set[tuple[str, str]],
    ] = {}
    technical_measurements: dict[
        tuple[_NumericExpression, str],
        set[str],
    ] = {}
    raw_expressions: dict[_NumericExpression, set[str]] = {}
    count_expressions: dict[_NumericExpression, set[str]] = {}
    technical_tokens: dict[str, set[tuple[int, str]]] = {}
    technical_token_sequences: dict[int, tuple[str, ...]] = {}

    for row_index, (item, raw_label) in enumerate(
        zip(basis_rows, basis_labels, strict=True)
    ):
        source_name = unicodedata.normalize("NFKC", str(item["name"]))
        source_value = unicodedata.normalize(
            "NFKC",
            str(item["value"]),
        ).strip()
        source_unit = _canonical_unit(str(item["unit"]))
        label = _normalized_analysis_label(raw_label)
        value_match = _NUMERIC_EXPRESSION_RE.fullmatch(source_value)
        value_expression = _numeric_expression(value_match) if value_match else None

        if value_expression is not None:
            variants = _source_expression_variants(value_expression, source_name)
            for variant in variants:
                if source_unit:
                    exact_measurements.setdefault(
                        (variant, source_unit),
                        set(),
                    ).add(label)
                else:
                    raw_expressions.setdefault(variant, set()).add(label)
                    if _COUNT_SOURCE_NAME_RE.search(source_name):
                        count_expressions.setdefault(variant, set()).add(label)
                source_spec = _POWER_UNIT_FACTORS.get(source_unit)
                if source_spec is not None and not variant.relation:
                    dimension, factor = source_spec
                    base_value = variant.values[0] * factor
                    power_conversions.setdefault(
                        (dimension, base_value, variant.comparator, variant.signs),
                        set(),
                    ).add((source_unit, label))

            for token in set(_RESTORABLE_ABBREVIATION_RE.findall(source_name)):
                if token in {"MPPT", "DCI", "GFCI", "AFCI", "STC", "NMOT"}:
                    technical_measurements.setdefault(
                        (value_expression, token),
                        set(),
                    ).add(label)

        for embedded in _MEASUREMENT_CLAIM_RE.finditer(source_name):
            embedded_expression = _numeric_expression(embedded)
            embedded_unit = _canonical_unit(embedded.group("unit"))
            if (
                embedded_expression is not None
                and (
                    _UNIT_ATOM_RE.fullmatch(embedded_unit)
                    or embedded_unit in _POWER_UNIT_FACTORS
                )
            ):
                exact_measurements.setdefault(
                    (embedded_expression, embedded_unit),
                    set(),
                ).add(label)

        if _COMPOSITE_TOPOLOGY_VALUE_RE.fullmatch(source_value):
            source_technical_tokens: tuple[str, ...] = ()
        else:
            combined_source = f"{source_name} {source_value}"
            source_technical_tokens = tuple(
                match.group()
                for match in _NUMERIC_TECHNICAL_TOKEN_RE.finditer(combined_source)
            )
        technical_token_sequences[row_index] = source_technical_tokens
        for token in source_technical_tokens:
            technical_tokens.setdefault(token, set()).add((row_index, label))

    return (
        exact_measurements,
        power_conversions,
        technical_measurements,
        raw_expressions,
        count_expressions,
        technical_tokens,
        technical_token_sequences,
    )


def _spans_overlap(
    left: tuple[int, int],
    right: tuple[int, int],
) -> bool:
    return left[0] < right[1] and right[0] < left[1]


def _span_is_covered(
    span: tuple[int, int],
    regions: Sequence[tuple[int, int]],
) -> bool:
    return any(start <= span[0] and span[1] <= end for start, end in regions)


def _bounded_descriptor(value: str, limit: int = 100) -> str:
    cleaned = " ".join(value.split())
    return cleaned if len(cleaned) <= limit else f"{cleaned[:limit - 3]}..."


def _missing_label_descriptor(
    descriptor: str,
    labels: Sequence[str],
) -> str:
    expected = sorted({label for label in labels if label})[:3]
    if not expected:
        return descriptor
    quoted = " or ".join(f'"{label}"' for label in expected)
    return _bounded_descriptor(
        f"{descriptor} must be immediately preceded by exact label {quoted}",
        220,
    )


def _claim_prefix_is_blocked(text: str, start: int) -> bool:
    prefix = text[:start]
    if _CLAIM_PREFIX_BLOCK_RE.search(prefix):
        return True
    stripped = prefix.rstrip()
    return bool(stripped and unicodedata.category(stripped[-1]) == "Sm")


def _claim_suffix_is_blocked(text: str, end: int) -> bool:
    suffix = text[end:]
    if _CLAIM_SUFFIX_BLOCK_RE.match(suffix):
        return True
    stripped = suffix.lstrip()
    return bool(stripped and unicodedata.category(stripped[0]) == "Sm")


def _count_classifier_is_professional(label: str, classifier: str) -> bool:
    if "MPPT" in label.upper():
        return classifier in {"个", "路", "组"}
    return classifier in {"个", "路", "组", "项", "套"}


def _reject_cross_row_arithmetic_claims(
    text: str,
    labels: Sequence[str],
    field: str,
) -> None:
    """Reject definite arithmetic assertions spanning cited rows."""

    active_labels = list(dict.fromkeys(label for label in labels if label))
    if len(active_labels) < 2:
        return
    for sentence_match in re.finditer(r"[^；;。！？!?]+", text):
        sentence = sentence_match.group()
        present_labels = {
            label for label in active_labels if label in sentence
        }
        multi_row_reference = len(present_labels) >= 2 or bool(
            present_labels and _MULTI_ROW_REFERENCE_RE.search(sentence)
        )
        if not multi_row_reference:
            continue
        label_spans = [
            label_match.span()
            for label in present_labels
            for label_match in re.finditer(re.escape(label), sentence)
        ]
        semantic_sentence = sentence
        for label in sorted(present_labels, key=len, reverse=True):
            semantic_sentence = semantic_sentence.replace(
                label,
                " " * len(label),
            )
        for arithmetic_match in _DERIVED_ARITHMETIC_RESULT_RE.finditer(
            semantic_sentence
        ):
            result_label_follows = any(
                start >= arithmetic_match.end()
                and start - arithmetic_match.end() <= 80
                for start, _end in label_spans
            )
            if result_label_follows:
                raise ParameterAnalysisError(
                    f"{field} contains a prohibited cross-row arithmetic result"
                )
            matched_text = arithmetic_match.group().rstrip()
            immediate_suffix = semantic_sentence[
                arithmetic_match.end() : arithmetic_match.end() + 16
            ]
            asks_for_unknown_result = bool(
                re.search(r"(?:达到|達到)$", matched_text)
                and re.match(r"\s*(?:何种|何種|如何|多少)", immediate_suffix)
            )
            explains_source = bool(
                re.search(r"是$", matched_text)
                and re.match(r"\s*由(?:于|於)?", immediate_suffix)
            )
            if asks_for_unknown_result or explains_source:
                continue
            raise ParameterAnalysisError(
                f"{field} contains a prohibited cross-row arithmetic result"
            )

def _reject_han_count_restatements(
    text: str,
    basis_rows: Sequence[Mapping[str, Any]],
    labels: Sequence[str],
    field: str,
) -> None:
    """Reject Chinese-number restatements of an actual count parameter."""

    for row, label in zip(basis_rows, labels, strict=True):
        source_name = unicodedata.normalize("NFKC", str(row["name"]))
        if not label or _COUNT_SOURCE_NAME_RE.search(source_name) is None:
            continue
        count_claim = re.compile(
            rf"{re.escape(label)}\s*(?:参数|方面)?\s*(?:被\s*)?"
            rf"{_COUNT_ASSIGNMENT_PATTERN}\s*"
            rf"(?:约|約|大约|大約)?\s*{_HAN_NUMBER_PATTERN}",
            re.IGNORECASE,
        )
        if count_claim.search(text):
            raise ParameterAnalysisError(
                f"{field} contains unsupported Chinese numeric text"
            )


def _measurement_spans(
    text: str,
    exact_measurements: Mapping[
        tuple[_NumericExpression, str],
        set[str],
    ],
    power_conversions: Mapping[
        tuple[str, Fraction, str, tuple[str, ...]],
        set[tuple[str, str]],
    ],
    technical_measurements: Mapping[
        tuple[_NumericExpression, str],
        set[str],
    ],
    technical_tokens: Mapping[str, set[tuple[int, str]]],
    technical_token_sequences: Mapping[int, tuple[str, ...]],
    all_labels: Sequence[str],
) -> tuple[
    list[tuple[int, int]],
    dict[tuple[int, int], str],
]:
    """Classify complete, locally labelled numeric expressions."""

    verified: list[tuple[int, int]] = []
    invalid: dict[tuple[int, int], str] = {}
    protected_regions: list[tuple[int, int]] = []
    verified_token_rows: list[tuple[int, set[int]]] = []
    token_cursors: dict[int, int] = {}
    token_matches = list(_NUMERIC_TECHNICAL_TOKEN_RE.finditer(text))

    for token_index, token_match in enumerate(token_matches):
        token = token_match.group()
        region = token_match.span()
        protected_regions.append(region)
        evidence = technical_tokens.get(token, set())
        direct_rows = {
            row_index
            for row_index, label in evidence
            if _label_is_locally_bound(
                text,
                (label,),
                token_match.start(),
                all_labels=all_labels,
            )
        }
        shared_rows: set[int] = set()
        if not direct_rows and verified_token_rows:
            previous_end, previous_rows = verified_token_rows[-1]
            gap = text[previous_end:token_match.start()]
            current_rows = {row_index for row_index, _label in evidence}
            if _TECHNICAL_TOKEN_SHARE_GAP_RE.fullmatch(gap):
                shared_rows = previous_rows & current_rows
        accepted_rows = direct_rows or shared_rows

        next_positions: dict[int, int] = {}
        for row_index in accepted_rows:
            sequence = technical_token_sequences.get(row_index, ())
            cursor = token_cursors.get(row_index, 0)
            for source_index in range(cursor, len(sequence)):
                if sequence[source_index] == token:
                    next_positions[row_index] = source_index + 1
                    break
        accepted_rows = set(next_positions)

        links_forward = False
        if accepted_rows and token_index + 1 < len(token_matches):
            next_match = token_matches[token_index + 1]
            gap = text[token_match.end():next_match.start()]
            next_rows = {
                row_index
                for row_index, _label in technical_tokens.get(
                    next_match.group(),
                    set(),
                )
            }
            links_forward = bool(
                accepted_rows & next_rows
                and _TECHNICAL_TOKEN_SHARE_GAP_RE.fullmatch(gap)
            )

        if (
            accepted_rows
            and (shared_rows or not _claim_prefix_is_blocked(text, token_match.start()))
            and (
                links_forward
                or not _claim_suffix_is_blocked(text, token_match.end())
            )
        ):
            verified.append(region)
            verified_token_rows.append((token_match.end(), accepted_rows))
            for row_index in accepted_rows:
                token_cursors[row_index] = next_positions[row_index]
        else:
            invalid[region] = _bounded_descriptor(token)

    for explicit in _EXPLICIT_UNIT_CLAIM_RE.finditer(text):
        region = explicit.span()
        protected_regions.append(region)
        invalid[region] = _bounded_descriptor(explicit.group())

    for claim in _MEASUREMENT_CLAIM_RE.finditer(text):
        region = claim.span()
        if any(_spans_overlap(region, item) for item in protected_regions):
            continue
        expression = _numeric_expression(claim)
        raw_unit = unicodedata.normalize("NFKC", claim.group("unit")).strip()
        unit = _canonical_unit(raw_unit)
        descriptor = _bounded_descriptor(claim.group())
        nonprofessional_power_case = (
            raw_unit.casefold() in _POWER_UNIT_CANONICAL
            and raw_unit not in _POWER_UNIT_FACTORS
        )
        if (
            expression is None
            or nonprofessional_power_case
            or _claim_prefix_is_blocked(text, claim.start())
            or _claim_suffix_is_blocked(text, claim.end())
        ):
            invalid[region] = descriptor
            continue

        technical_labels = technical_measurements.get((expression, unit), set())
        if technical_labels:
            if _label_is_locally_bound(
                text,
                tuple(technical_labels),
                claim.start(),
                all_labels=all_labels,
            ):
                verified.append(region)
                continue
            invalid[region] = _missing_label_descriptor(
                descriptor,
                technical_labels,
            )
            continue

        labels = exact_measurements.get((expression, unit), set())
        if labels:
            if _label_is_locally_bound(
                text,
                tuple(labels),
                claim.start(),
                all_labels=all_labels,
            ):
                verified.append(region)
                continue
            invalid[region] = _missing_label_descriptor(descriptor, labels)
            continue

        output_spec = _POWER_UNIT_FACTORS.get(unit)
        if output_spec is not None and not expression.relation:
            dimension, factor = output_spec
            base_value = expression.values[0] * factor
            evidence = power_conversions.get(
                (dimension, base_value, expression.comparator, expression.signs),
                set(),
            )
            labels = tuple(
                label
                for source_unit, label in evidence
                if source_unit != unit and label
            )
            if labels:
                if _label_is_locally_bound(
                    text,
                    labels,
                    claim.start(),
                    all_labels=all_labels,
                ):
                    verified.append(region)
                    continue
                invalid[region] = _missing_label_descriptor(descriptor, labels)
                continue
        invalid[region] = descriptor

    occupied = [*protected_regions, *verified, *invalid]
    for pattern in (
        _HAN_NUMERIC_UNIT_RE,
        _HAN_BRACKETED_UNIT_RE,
        _HAN_CLASSIFIED_COUNT_RE,
        _HAN_ENGINEERING_TOPOLOGY_RE,
        _HAN_DERIVED_METRIC_RE,
        _HAN_IMPLICIT_DERIVED_METRIC_RE,
        _HAN_COUNT_CIRCUMLOCUTION_RE,
        _ENGLISH_DERIVED_METRIC_RE,
        _HAN_COUNT_CLAIM_RE,
    ):
        for han_claim in pattern.finditer(text):
            region = han_claim.span()
            if any(_spans_overlap(region, item) for item in occupied):
                continue
            invalid[region] = _bounded_descriptor(han_claim.group())
            occupied.append(region)
    return verified, invalid


def _unreferenced_parameter_label_ids(
    text_values: Sequence[str],
    referenced_ids: Sequence[str],
    labels_by_id: Mapping[str, str],
    field: str,
) -> list[str]:
    """Return safely resolvable IDs for uncited complete label mentions."""

    ids_by_label: dict[str, list[str]] = {}
    for parameter_id, label in labels_by_id.items():
        if label:
            ids_by_label.setdefault(label, []).append(parameter_id)

    referenced = set(referenced_ids)
    additional_ids: list[str] = []
    for raw_text in text_values:
        text = unicodedata.normalize("NFKC", raw_text)
        matches = [
            (match.start(), match.end(), label)
            for label in ids_by_label
            for match in re.finditer(re.escape(label), text)
        ]
        maximal_matches = [
            candidate
            for candidate in matches
            if not any(
                (other[1] - other[0]) > (candidate[1] - candidate[0])
                and _span_is_covered(
                    (candidate[0], candidate[1]),
                    [(other[0], other[1])],
                )
                for other in matches
            )
        ]
        overlapping_ids: list[str] = []
        for index, candidate in enumerate(maximal_matches):
            candidate_span = (candidate[0], candidate[1])
            for other in maximal_matches[index + 1:]:
                if not _spans_overlap(
                    candidate_span,
                    (other[0], other[1]),
                ):
                    continue
                overlapping_ids.extend(ids_by_label[candidate[2]])
                overlapping_ids.extend(ids_by_label[other[2]])
        if overlapping_ids:
            raise ParameterAnalysisError(
                f"{field} contains overlapping complete parameter labels "
                "that cannot be mapped unambiguously to IDs: "
                f"{', '.join(list(dict.fromkeys(overlapping_ids))[:8])}"
            )

        for _start, _end, label in sorted(maximal_matches):
            parameter_ids = ids_by_label[label]
            if len(parameter_ids) != 1:
                if set(parameter_ids).issubset(referenced):
                    continue
                raise ParameterAnalysisError(
                    f"{field} contains a complete parameter label that maps "
                    "to ambiguous IDs: "
                    f"{', '.join(parameter_ids[:8])}"
                )
            if referenced.intersection(parameter_ids):
                continue
            parameter_id = parameter_ids[0]
            referenced.add(parameter_id)
            additional_ids.append(parameter_id)
    return additional_ids


def _ground_numbers(
    text_values: Sequence[str],
    basis_rows: Sequence[Mapping[str, Any]],
    field: str,
    *,
    basis_labels: Sequence[str] | None = None,
) -> None:
    if basis_labels is None:
        labels = [""] * len(basis_rows)
    else:
        labels = list(basis_labels)
        if len(labels) != len(basis_rows):
            raise ValueError("basis labels must align with basis rows")
    normalized_labels = [
        _normalized_analysis_label(label)
        for label in labels
    ]
    duplicate_labels = {
        label
        for label in normalized_labels
        if label and normalized_labels.count(label) > 1
    }
    if duplicate_labels:
        raise ParameterAnalysisError(
            f"{field} basis labels are ambiguous within one paragraph: "
            f"{', '.join(sorted(duplicate_labels)[:8])}"
        )

    source_text = unicodedata.normalize(
        "NFKC",
        " ".join(
            f"{item['name']} {item['value']} {item['unit']}"
            for item in basis_rows
        ),
    )
    supported_in_order = list(
        dict.fromkeys(_FALLBACK_NUMBER_RE.findall(source_text))
    )
    (
        exact_measurements,
        power_conversions,
        technical_measurements,
        raw_expressions,
        count_expressions,
        technical_tokens,
        technical_token_sequences,
    ) = _basis_measurement_evidence(basis_rows, normalized_labels)

    unsupported: list[str] = []
    invalid_measurements: list[str] = []
    for raw_text in text_values:
        text = unicodedata.normalize("NFKC", raw_text)
        _reject_han_count_restatements(
            text,
            basis_rows,
            normalized_labels,
            field,
        )
        _reject_cross_row_arithmetic_claims(text, normalized_labels, field)
        verified_regions, invalid_regions = _measurement_spans(
            text,
            exact_measurements,
            power_conversions,
            technical_measurements,
            technical_tokens,
            technical_token_sequences,
            normalized_labels,
        )
        occupied_regions = [*verified_regions, *invalid_regions]
        for region, descriptor in invalid_regions.items():
            invalid_measurements.append(descriptor)
            unsupported.extend(
                _ANY_DIGIT_RE.findall(text[region[0] : region[1]])
            )

        raw_regions: list[tuple[int, int]] = []
        for match in _NUMERIC_EXPRESSION_SCAN_RE.finditer(text):
            initial_region = match.span()
            if _span_is_covered(initial_region, occupied_regions) or any(
                _spans_overlap(initial_region, occupied)
                for occupied in occupied_regions
            ):
                continue
            expression = _numeric_expression(match)
            classifier_match = _COUNT_CLASSIFIER_RE.match(text[match.end():])
            if classifier_match is not None:
                region = (
                    match.start(),
                    match.end() + classifier_match.end(),
                )
                classifier = classifier_match.group("classifier")
                allowed_labels = {
                    label
                    for label in count_expressions.get(expression, set())
                    if _count_classifier_is_professional(label, classifier)
                }
            else:
                region = initial_region
                allowed_labels = raw_expressions.get(expression, set())
            raw_regions.append(region)
            suffix = text[region[1]:]
            valid_raw = (
                expression is not None
                and bool(allowed_labels)
                and not _claim_prefix_is_blocked(text, match.start())
                and not _claim_suffix_is_blocked(text, region[1])
                and _RAW_CLAIM_SUFFIX_BLOCK_RE.match(suffix) is None
                and _label_is_locally_bound(
                    text,
                    tuple(allowed_labels),
                    match.start(),
                    all_labels=normalized_labels,
                )
            )
            if not valid_raw:
                descriptor = _bounded_descriptor(text[region[0]:region[1]])
                invalid_measurements.append(descriptor)
                unsupported.extend(_ANY_DIGIT_RE.findall(descriptor))

        all_regions = [*occupied_regions, *raw_regions]
        for match in _ANY_DIGIT_RE.finditer(text):
            if not _span_is_covered(match.span(), all_regions):
                unsupported.append(match.group())
                invalid_measurements.append(_bounded_descriptor(match.group()))

    if unsupported or invalid_measurements:
        unsupported_text = ", ".join(
            _bounded_descriptor(item, 32)
            for item in list(dict.fromkeys(unsupported))[:8]
        )
        supported_text = ", ".join(
            _bounded_descriptor(item, 32)
            for item in supported_in_order[:16]
        ) or "none"
        invalid_text = ", ".join(
            list(dict.fromkeys(invalid_measurements))[:8]
        )
        invalid_suffix = (
            f"; invalid measurements: {invalid_text}" if invalid_text else ""
        )
        raise ParameterAnalysisError(
            f"{field} contains numeric text not present in its basis parameters; "
            f"unsupported: {unsupported_text or 'none'}; "
            f"basis permits: {supported_text}{invalid_suffix}"
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
    translation_by_id = {
        item["parameter_id"]: item for item in translations
    }
    narrative_label_by_id = {
        parameter_id: _normalized_analysis_label(item["name_zh"])
        for parameter_id, item in translation_by_id.items()
    }

    sections_raw = raw.get("sections")
    minimum_sections = 5 if len(source_rows) >= 30 else (
        3 if len(source_rows) >= 10 else 1
    )
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
            if not isinstance(basis_ids, list):
                raise ParameterAnalysisError(
                    f"{paragraph_prefix}.basis_parameter_ids must be an array"
                )
            if not minimum_basis <= len(basis_ids) <= 8:
                raise ParameterAnalysisError(
                    f"{paragraph_prefix}.basis_parameter_ids must contain "
                    f"{minimum_basis}-8 IDs; received {len(basis_ids)}"
                )
            non_strings = [
                str(index)
                for index, item in enumerate(basis_ids)
                if not isinstance(item, str)
            ]
            if non_strings:
                raise ParameterAnalysisError(
                    f"{paragraph_prefix}.basis_parameter_ids must contain only "
                    f"string IDs; invalid indexes: {', '.join(non_strings[:8])}"
                )
            normalized_basis_ids = list(basis_ids)
            malformed_indexes = [
                str(index)
                for index, item in enumerate(normalized_basis_ids)
                if re.fullmatch(r"p\d{3}", item) is None
            ]
            if malformed_indexes:
                raise ParameterAnalysisError(
                    f"{paragraph_prefix}.basis_parameter_ids must match pNNN; "
                    f"invalid indexes: {', '.join(malformed_indexes[:8])}"
                )
            duplicate_ids = [
                item
                for index, item in enumerate(normalized_basis_ids)
                if item in normalized_basis_ids[:index]
            ]
            if duplicate_ids:
                raise ParameterAnalysisError(
                    f"{paragraph_prefix}.basis_parameter_ids contains duplicate "
                    f"IDs: {', '.join(dict.fromkeys(duplicate_ids))}"
                )
            unknown_ids = [
                item for item in normalized_basis_ids if item not in source_by_id
            ]
            if unknown_ids:
                raise ParameterAnalysisError(
                    f"{paragraph_prefix}.basis_parameter_ids contains unknown IDs: "
                    f"{', '.join(unknown_ids[:8])}; valid IDs are "
                    f"p001-p{len(source_rows):03d}"
                )
            basis_ids = normalized_basis_ids
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
            paragraph_text_values = [analysis_zh, *conditions, *limitations]
            additional_basis_ids = _unreferenced_parameter_label_ids(
                paragraph_text_values,
                basis_ids,
                narrative_label_by_id,
                paragraph_prefix,
            )
            if additional_basis_ids:
                if len(basis_ids) + len(additional_basis_ids) > 8:
                    raise ParameterAnalysisError(
                        f"{paragraph_prefix} contains uncited complete parameter "
                        "labels, but adding their IDs would exceed the 8-ID "
                        "basis_parameter_ids limit: "
                        f"{', '.join(additional_basis_ids[:8])}"
                    )
                basis_ids = [*basis_ids, *additional_basis_ids]
            basis_rows = [source_by_id[item] for item in basis_ids]
            basis_labels = [
                translation_by_id[item]["name_zh"] for item in basis_ids
            ]
            _ground_numbers(
                paragraph_text_values,
                basis_rows,
                paragraph_prefix,
                basis_labels=basis_labels,
            )
            narrative_labels = [
                _normalized_analysis_label(label)
                for label in basis_labels
            ]
            if analysis_kind != "limitation" and not any(
                label and label in unicodedata.normalize("NFKC", analysis_zh)
                for label in narrative_labels
            ):
                raise ParameterAnalysisError(
                    f"{paragraph_prefix}.analysis_zh must name at least one "
                    "referenced parameter label"
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
    "parameter_numeric_guidance",
    "parameter_id",
    "parameter_set_sha256",
    "professional_analysis",
    "validate_parameter_enrichment",
]
