"""Normalize an explicitly labelled release date, never publication/update dates.

Month precision is retained as an English month/year, not invented as day 1.
"""
import calendar
from datetime import date
import re


def normalize_release_date(raw):
    if not isinstance(raw, str):
        return None
    value = re.sub(r'\s+', ' ', raw).strip()
    # Kimovil's relative age is a presentation suffix, not date evidence.
    value = re.sub(r',\s*\d+\s+(?:days?|weeks?|months?|years?)\s+ago\s*$', '', value, flags=re.I)
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            return None
    months = {month.casefold(): month for month in calendar.month_name if month}
    match = re.fullmatch(r'([A-Za-z]+) (20\d{2})', value)
    if match and match[1].casefold() in months:
        return f'{months[match[1].casefold()]} {match[2]}'
    # Unqualified years, relative dates, "coming soon", and locale-ambiguous
    # numeric dates must not silently acquire precision or availability.
    return None
