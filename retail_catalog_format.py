"""Lossless on-disk grouping of repeated retail field provenance metadata."""
import json


def decode_product(product):
    if not isinstance(product, dict) or 'specSourceGroups' not in product:
        return product
    result = {key: value for key, value in product.items() if key != 'specSourceGroups'}
    sources = {}
    groups = product['specSourceGroups']
    if not isinstance(groups, list):
        raise ValueError('specSourceGroups must be an array')
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get('fields'), list):
            raise ValueError('Invalid specification source group')
        metadata = {key: value for key, value in group.items() if key != 'fields'}
        for field in group['fields']:
            if not isinstance(field, str):
                raise ValueError('Specification source group fields must be strings')
            if field in sources and sources[field] != metadata:
                raise ValueError('Conflicting grouped provenance for one field')
            sources[field] = dict(metadata)
    # Explicit legacy fields take precedence when both formats are present.
    explicit = product.get('specSources', {})
    if not isinstance(explicit, dict):
        raise ValueError('specSources must be an object when groups are present')
    result['specSources'] = {**sources, **explicit}
    return result


def encode_product(product):
    product = decode_product(product)
    if not isinstance(product, dict) or not isinstance(product.get('specSources'), dict) or not product['specSources']:
        return product
    groups = {}
    for field, metadata in product['specSources'].items():
        # The reserved group key cannot represent an identically named metadata
        # property, so retain the legacy format in that exceptional case.
        if not isinstance(metadata, dict) or 'fields' in metadata:
            return product
        key = json.dumps(metadata, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
        if key not in groups:
            groups[key] = {**metadata, 'fields': []}
        groups[key]['fields'].append(field)
    result = {key: value for key, value in product.items() if key != 'specSources'}
    result['specSourceGroups'] = list(groups.values())
    return result


def decode_catalog(value):
    return [decode_product(product) for product in value] if isinstance(value, list) else value


def encode_catalog(value):
    return [encode_product(product) for product in value] if isinstance(value, list) else value
