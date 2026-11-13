"""Obtain short-lived GitHub Actions credentials for a site document reader."""

import os
from threading import Lock
from time import monotonic

import requests


def actions_collector_token(audience):
    endpoint = os.getenv('ACTIONS_ID_TOKEN_REQUEST_URL')
    credential = os.getenv('ACTIONS_ID_TOKEN_REQUEST_TOKEN')
    if not endpoint or not credential:
        raise RuntimeError('GitHub Actions OIDC access is unavailable')
    token, refresh_at = '', 0
    lock = Lock()

    def get_token():
        nonlocal token, refresh_at
        with lock:
            if monotonic() >= refresh_at:
                response = requests.get(endpoint, params={'audience': audience}, timeout=12,
                                        headers={'Authorization': f'Bearer {credential}'}, allow_redirects=False)
                if response.status_code != 200:
                    raise RuntimeError(f'GitHub Actions OIDC request failed (HTTP {response.status_code})')
                value = response.json().get('value')
                if not isinstance(value, str) or len(value) < 32:
                    raise RuntimeError('GitHub Actions returned an invalid OIDC credential')
                token, refresh_at = value, monotonic() + 180
            return token

    return get_token
