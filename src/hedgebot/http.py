"""带退避重试的 HTTP。"""
from __future__ import annotations

import time

import requests

TIMEOUT = 20


class SourceError(RuntimeError):
    """某个数据源取数失败。"""


def request(method: str, url: str, label: str, *, attempts: int = 3, **kwargs):
    """5xx / 网络错误重试；4xx 直接报错（重试也不会变）。

    私有接口要传 attempts=1 —— Kraken 的 nonce 只能用一次，原样重发必回
    EAPI:Invalid nonce，看起来像密钥错了。
    """
    last = ""
    for i in range(attempts):
        try:
            r = requests.request(method, url, timeout=TIMEOUT, **kwargs)
            if r.status_code == 429:
                # 限流按分钟窗口算，秒级退避会撞在同一个窗口里
                last = "HTTP 429"
                ra = r.headers.get("Retry-After")
                if i + 1 < attempts:
                    time.sleep(float(ra) if ra and ra.isdigit() else 15 * (i + 1))
                continue
            if r.status_code >= 500:
                last = f"HTTP {r.status_code}"
            elif r.status_code >= 400:
                raise SourceError(f"{label}: HTTP {r.status_code} {r.text[:200]}")
            else:
                return r.json()
        except (requests.RequestException, ValueError) as e:
            last = str(e)
        if i + 1 < attempts:
            time.sleep(1.5 * (i + 1))
    raise SourceError(f"{label} 请求失败（{attempts} 次）: {last}")
