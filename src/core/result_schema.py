"""One source of validation rules for live analysis, resume, and evaluation."""
import re
from datetime import date

from .config import STAGE1_FIELDS, STAGE2_FIELDS, STAGE3_FIELDS, GEO_FIELDS

STAGE_FIELDS = {"stage1": STAGE1_FIELDS, "stage2": STAGE2_FIELDS, "stage3": STAGE3_FIELDS}
HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\U00020000-\U0002fa1f]+")
FORBIDDEN_LABELS = {"自然地理学", "人文地理学", "地理信息科学", "地理信息系统", "城市规划", "遥感", "卫星导航系统"}


def valid_deadline(value):
    if value in ("", "Soon"):
        return True
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


def discard_overlong_labels(result):
    """Reject only the offending value; never truncate or discard other fields."""
    if not isinstance(result, dict):
        return result, []
    cleaned, warnings = dict(result), []
    for field in ('WX_Label1', 'WX_Label2', 'WX_Label3', 'WX_Label4', 'WX_Label5'):
        value = cleaned.get(field)
        if isinstance(value, str) and len(value) > 6:
            cleaned[field] = ''
            warnings.append({'stage': 'stage3', 'field': field, 'value': value,
                             'reason': 'overlong_label', 'length': len(value)})
    return cleaned, warnings


def rejected_primary_label(warnings):
    if not isinstance(warnings, list):
        return False
    return any(isinstance(item, dict) and item.get('field') == 'WX_Label1'
               and item.get('reason') == 'overlong_label'
               and isinstance(item.get('value'), str) and len(item['value']) > 6
               for item in (warnings or []))


def validation_errors(result, stage, *, require_geo=True, allow_empty_label1=False):
    if stage not in STAGE_FIELDS:
        return [f"未知阶段: {stage}"]
    if not isinstance(result, dict):
        return ["结果必须是 JSON 对象"]
    fields = STAGE_FIELDS[stage]
    errors = []
    unknown = sorted(str(key) for key in result if key not in fields)
    if unknown:
        errors.append(f"未知字段: {', '.join(map(str, unknown))}")
    if stage == "stage2":
        if any(value != "1" for value in result.values()):
            errors.append("分类字段的值必须是字符串 '1'")
        count = sum(result.get(field) == "1" for field in GEO_FIELDS)
        if count > 3 or (require_geo and count < 1):
            errors.append("专业方向必须为 1–3 个")
        return errors
    missing = [field for field in fields if field not in result]
    if missing:
        errors.append(f"缺少字段: {', '.join(missing)}")
    wrong_types = [key for key in fields if key in result and not isinstance(result[key], str)]
    if wrong_types:
        errors.append(f"字段必须是字符串: {', '.join(wrong_types)}")
    if missing or wrong_types:
        return errors
    if stage == "stage1":
        if not valid_deadline(result["Deadline"]):
            errors.append("Deadline 必须为空、Soon 或有效的 YYYY-MM-DD 日期")
        places = result["Number_Places"]
        if places and not re.fullmatch(r"[1-9]\d*", places):
            errors.append("Number_Places 必须为空或正整数字符串")
    else:
        for field in ("University_CN", "Country_CN"):
            if result[field] and not HAN.fullmatch(result[field]):
                errors.append(f"{field} 必须为空或中文名称（不含英文、数字和符号）")
        for field in ("WX_Label1", "WX_Label2", "WX_Label3"):
            value = result[field]
            if field == 'WX_Label1' and not value and not allow_empty_label1:
                errors.append('WX_Label1 为空，必须填写至少一个专业标签')
            if len(value) > 6:
                errors.append(f'{field} 为 {len(value)} 字，超过六字上限')
            if value and not HAN.fullmatch(value):
                errors.append(f'{field} 必须仅含汉字')
            if value in FORBIDDEN_LABELS:
                errors.append(f'{field} 使用了不允许的通用分类标签: {value}')
        if result["WX_Label4"] or result["WX_Label5"]:
            errors.append("WX_Label4 和 WX_Label5 必须留空")
    return errors
