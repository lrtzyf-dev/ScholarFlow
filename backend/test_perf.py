# -*- coding: utf-8 -*-
"""2337 chunks 真实库下的检索性能与内存测试。"""
import os
import time

import numpy as np
import psutil

from app.db import SessionLocal
from app.rag.retriever import dense_search, retrieve, fts_search

QUERIES = [
    "residual connection deep network degradation",
    "masked language model pre-training",
    "contrastive learning image text alignment",
    "preference optimization without reinforcement learning",
    "scaled dot product attention multi-head",
]


def main():
    proc = psutil.Process(os.getpid())
    db = SessionLocal()
    mem0 = proc.memory_info().rss / 1024 / 1024
    print(f"初始内存: {mem0:.1f} MB")

    # 单次 dense 检索计时（对 2337 个 512 维向量）
    dtimes, ftimes, rtimes = [], [], []
    for q in QUERIES:
        t = time.perf_counter()
        dh = dense_search(q, 24, None)
        dtimes.append((time.perf_counter() - t) * 1000)

        t = time.perf_counter()
        fh = fts_search(db, q, 24, None)
        ftimes.append((time.perf_counter() - t) * 1000)

        t = time.perf_counter()
        ev = retrieve(db, q, 6)
        rtimes.append((time.perf_counter() - t) * 1000)

    mem1 = proc.memory_info().rss / 1024 / 1024
    print(f"dense检索(ms): 均值{np.mean(dtimes):.2f} 最大{np.max(dtimes):.2f} 最小{np.min(dtimes):.2f}")
    print(f"FTS检索(ms):   均值{np.mean(ftimes):.2f} 最大{np.max(ftimes):.2f}")
    print(f"整体retrieve(ms)含MMR: 均值{np.mean(rtimes):.2f} 最大{np.max(rtimes):.2f} 最小{np.min(rtimes):.2f}")
    print(f"结束内存: {mem1:.1f} MB  增量: {mem1-mem0:.1f} MB")
    print(f"最后一次retrieve返回证据数: {len(ev)}")
    db.close()


if __name__ == "__main__":
    main()
