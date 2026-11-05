"""Exact selected n11 storage units, independent of catalog expectations."""
import re


def storage_gb(label):
    if not isinstance(label, str):
        return None
    match = re.fullmatch(r'([1-9]\d{0,3}) (GB|TB)', label)
    if match is None:
        return None
    amount = int(match[1]) * (1024 if match[2] == 'TB' else 1)
    return amount if 32 <= amount <= 4096 else None


def storage_selector_gb(selector):
    if not isinstance(selector, str):
        return None
    match = re.fullmatch(r'(32|64|128|256|512|1024|2048|4096)-gb|(1|2|4)-tb', selector)
    if match is None:
        return None
    return int(match[1]) if match[1] else int(match[2]) * 1024
