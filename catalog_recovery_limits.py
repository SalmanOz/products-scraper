"""Whole-catalog admission, separate from retailer traffic and write budgets."""

# Automatic verified memory siblings are now part of the real catalog. Reject
# an oversized response as a whole; never slice it to make coverage pass.
# This does not increase any source quota or the 12-variant publication limit.
MAX_RECOVERY_CATALOG_PRODUCTS = 300
