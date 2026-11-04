"""Exact public storage labels; no capacity is supplied by catalog defaults."""
import re


def capacity_gb(label):
    if not isinstance(label, str):
        return None
    match = re.fullmatch(r'([1-9]\d{0,3}) (GB|TB)', label)
    if match is None:
        return None
    value = int(match[1]) * (1024 if match[2] == 'TB' else 1)
    return value if 32 <= value <= 4096 else None


def selected_tb_storage(soup, selected_detail):
    """Actual single native cell, never the first amount in a choice/range.

    Keep identical duplicate selected-table support, but a conflicting table,
    extra row/capacity or non-native structure cannot supply TB evidence.
    The caller must still prove the entire selected SKU, seller, stock and cash.
    """
    from hepsiburada_s25_fe_source import _normalized
    tables = soup.select('#techSpecs[data-test-id="KeyFeaturesTable"]')
    if not 1 <= len(tables) <= 2 or not selected_detail:
        return None
    amounts = []
    for table in tables:
        if _normalized(table.get_text(' ', strip=True)) != selected_detail:
            return None
        labels = table.find_all(string=lambda text: isinstance(text, str) and _normalized(text) == 'Dahili Hafıza')
        if len(labels) != 1:
            return None
        label = labels[0].parent
        row = label.parent
        cells = row.find_all(recursive=False) if row else []
        if (label.name != 'div' or len(cells) != 2 or cells[0] is not label
                or cells[1].name != 'div'):
            return None
        value = _normalized(cells[1].get_text(' ', strip=True))
        if not re.fullmatch(r'[1-9]\d{0,3} TB', value):
            return None
        amount = capacity_gb(value)
        if amount is None:
            return None
        amounts.append(amount)
    return amounts[0] if len(set(amounts)) == 1 else None
