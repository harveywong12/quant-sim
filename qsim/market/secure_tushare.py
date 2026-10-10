"""Explicit HTTPS-only Tushare transport (requests; no SDK private mutation).

A working authenticated HTTPS endpoint must be verified locally before use.
No default is guessed, no redirects are followed, no downgrade is attempted.
The JSON request/response contract follows Tushare 1.4.29's DataApi.query.
"""
from __future__ import annotations
import os
import re
import threading
import time
from urllib.parse import urlsplit
import pandas as pd
import requests


class ProviderError(RuntimeError):
    pass


class HTTPSClient:
    def __init__(self, token: str, url: str, interval: float = 0.7):
        parts = urlsplit(url)
        if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError('Tushare endpoint must be an explicit HTTPS URL without credentials/query/fragment')
        if not token or interval < .7:
            raise ValueError('token required; request interval must be >=0.7 seconds')
        self._token = token
        self._url = url.rstrip('/')
        self._interval = interval
        self._lock = threading.Lock()
        self._last_request = 0.

    def query(self, api_name: str, fields: str = '', **params):
        if not re.fullmatch(r'[a-z][a-z0-9_]*', api_name):
            raise ValueError('invalid API name')
        with self._lock:
            time.sleep(max(0, self._interval - (time.monotonic() - self._last_request)))
            self._last_request = time.monotonic()
            try:
                response = requests.post(self._url+'/'+api_name,
                    json={'api_name': api_name, 'token': self._token, 'params': params, 'fields': fields},
                    timeout=(10,30), verify=True, allow_redirects=False)
                if not 200 <= response.status_code < 300:
                    raise ProviderError('Tushare HTTPS request failed; no downgrade/redirect allowed')
                result = response.json()
                if result.get('code') != 0 or not isinstance(result.get('data'),dict):
                    raise ProviderError('provider rejected request or returned invalid response')
                data = result['data']
                return pd.DataFrame(data['items'], columns=data['fields'])
            except (requests.RequestException, ValueError, KeyError, TypeError):
                # Do not propagate a response or exception containing token/body.
                raise ProviderError('Tushare HTTPS transport/response failed') from None


def make_client(token: str) -> HTTPSClient:
    url = os.environ.get('QSIM_TUSHARE_HTTPS_URL','').strip()
    if not url:
        raise RuntimeError('需配置并本地验证 QSIM_TUSHARE_HTTPS_URL；禁止默认明文 HTTP 客户端')
    return HTTPSClient(token, url)
