"""确定性灰度分桶。

规则（需求给定，禁止随机）：
    bucket = int(sha256(record_id 字节).hexdigest() 前 8 个十六进制字符, 16) % 100
    bucket < canary_percent → 新版本，否则旧版本。
同一 record_id 重跑永远落到同一桶；切换百分比只改变桶边界，不改变哈希。
"""
from __future__ import annotations

import hashlib


def bucket_of(record_id: str) -> int:
    """返回记录编号对应的确定性桶号 [0, 99]。"""
    digest = hashlib.sha256(str(record_id).encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100


def use_candidate(record_id: str, canary_percent: int) -> bool:
    """该记录在当前灰度百分比下是否走候选新版本。"""
    if not 0 <= canary_percent <= 100:
        raise ValueError(f"灰度百分比必须在 0..100 之间: {canary_percent}")
    return bucket_of(record_id) < canary_percent
