"""Discord + Pushover。凭据与 perp-track 共用（systemd 同时加载两边的 .env）。

两个通道各自独立：一个挂了不影响另一个。只要有一个送达就算送达 ——
watch 据此决定是否记「已发」，都没送达则下一轮重试。
"""
from __future__ import annotations

import os
import time

import requests

COLORS = {1: 0xD7301F, 0: 0xFC8D59, -1: 0x1A9850}   # 红 / 橙 / 绿


def _discord(title: str, message: str, priority: int) -> str | None:
    url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        return "未配置"
    payload = {"embeds": [{"title": title, "description": message[:4000],
                           "color": COLORS.get(priority, 0x2B8CBE)}]}
    last = ""
    for i in range(3):
        try:
            r = requests.post(url, json=payload, timeout=10)
            if r.status_code < 300:
                return None
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last = str(e)
        time.sleep(2 * (i + 1))
    return last


def _pushover(title: str, message: str, priority: int) -> str | None:
    tok = os.environ.get("PUSHOVER_TOKEN", "").strip()
    usr = os.environ.get("PUSHOVER_USER", "").strip()
    if not (tok and usr):
        return "未配置"
    last = ""
    for i in range(3):
        try:
            r = requests.post("https://api.pushover.net/1/messages.json", timeout=15, data={
                "token": tok, "user": usr, "title": title[:250],
                "message": message[:1024], "priority": priority})
            if r.status_code < 300:
                return None
            if 400 <= r.status_code < 500:
                return f"HTTP {r.status_code} {r.text[:200]}"
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last = str(e)
        time.sleep(2 * (i + 1))
    return last


def send(title: str, message: str, priority: int = 0) -> tuple[bool, list[str]]:
    """priority：1 = 穿透勿扰（真有裸敞口）、0 = 普通、-1 = 安静（恢复通知）。

    返回 (是否至少一个通道送达, 各通道失败原因)。
    """
    fails = []
    ok = False
    for name, fn in (("Discord", _discord), ("Pushover", _pushover)):
        err = fn(title, message, priority)
        if err is None:
            ok = True
        else:
            fails.append(f"{name}: {err}")
    return ok, fails
