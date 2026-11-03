"""Normalize checksum-valid UPC-A/GTIN-12 to its equivalent zero-prefixed GTIN-13.

GS1: https://ref.gs1.org/standards/genspecs/23.0.0/ section 2.1.1.10.
Keep the observed retailer barcode alongside the normalized identifier. Never
pad arbitrary lengths, repair checksums, or choose one of multiple barcodes.
"""
import re
from mediamarkt_marketplace_audit import _gtin13


def retailer_gtin13(raw):
    if not isinstance(raw, str) or not re.fullmatch(r'\d{12,13}', raw):
        return None
    return _gtin13(('0' + raw) if len(raw) == 12 else raw)
