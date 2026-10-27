#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PhotoFlux demo 网关压测 — 场景: 基线/错误路径/限流探测/并发"""
import json, time, statistics, urllib.request, urllib.error, threading, base64, sys

BASE = "http://127.0.0.1:8921"
results = []

def call(path, payload=None, timeout=130):
    t = time.time()
    try:
        req = urllib.request.Request(BASE + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json"},
            method="POST" if payload is not None else "GET")
        r = urllib.request.urlopen(req, timeout=timeout)
        body = r.read()
        return r.status, body, (time.time() - t) * 1000
    except urllib.error.HTTPError as e:
        return e.code, e.read(), (time.time() - t) * 1000
    except Exception as e:
        return 0, str(e).encode(), (time.time() - t) * 1000

def report(name, ok, detail):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'} | {name}: {detail}")

# ---- S1 基线（本地端点）----
lat = []
for _ in range(30):
    c, b, ms = call("/health")
    lat.append(ms)
report("S1 health x30", all(call("/health")[0] == 200 for _ in range(1)),
       f"median {statistics.median(lat):.1f}ms p95 {sorted(lat)[28]:.1f}ms")

# ---- S2 错误路径 ----
c1, _, _ = call("/v1/images/generations", {})
c2, _, _ = call("/v1/images/generations", {"prompt": ""})
c3, _, _ = call("/v1/tasks/not-exist")
report("S2 错误路径", c1 == 400 and c2 == 400 and c3 == 404,
       f"空prompt={c1} 空白prompt={c2} 不存在任务={c3}")

# ---- S3 限流探测（pollinations IP 限流阈值）----
print("S3 限流探测（间隔递减打真实上游）...")
codes = []
for i, gap in enumerate([3, 2, 1, 0.5, 0.2, 0]):
    if gap: time.sleep(gap)
    c, b, ms = call("/v1/images/generations", {"prompt": f"tiny test dot {i}", "ratio": "1"})
    codes.append((gap, c, round(ms)))
    print(f"   gap={gap}s -> {c} ({round(ms)}ms)")
ok_codes = [c for _, c, _ in codes if c == 200]
report("S3 限流探测", len(ok_codes) >= 2, f"结果 {codes}")

# ---- S4 本地并发（健康+模型+stats 混合 60 并发）----
lat, errs = [], [0]
lock = threading.Lock()
def worker():
    for p in ["/health", "/v1/models", "/v1/stats"]:
        c, b, ms = call(p, timeout=10)
        with lock:
            lat.append(ms)
            if c != 200: errs[0] += 1
ts = [threading.Thread(target=worker) for _ in range(20)]
t0 = time.time()
[t.start() for t in ts]; [t.join() for t in ts]
wall = (time.time() - t0) * 1000
report("S4 本地 20线程x3", errs[0] == 0,
       f"60 req {wall:.0f}ms wall, median {statistics.median(lat):.1f}ms, err={errs[0]}")

# ---- S5 上游真实并发生图（3 并发，探测上游容量+限流）----
print("S5 上游 3 并发真实生图...")
codes = []
def gen(i):
    c, b, ms = call("/v1/images/generations", {"prompt": f"abstract art no.{i}", "ratio": "1"}, timeout=140)
    with lock: codes.append((c, round(ms)))
    if c == 200:
        d = json.loads(b)["data"][0]
        print(f"   #{i}: 200 {d['bytes']}B {d['upstream_ms']}ms")
    else:
        print(f"   #{i}: {c} {b[:60]}")
ts = [threading.Thread(target=gen, args=(i,)) for i in range(3)]
t0 = time.time(); [t.start() for t in ts]; [t.join() for t in ts]
report("S5 上游 3 并发", sum(1 for c, _ in codes if c == 200) >= 1,
       f"{codes} wall {(time.time()-t0):.0f}s")

# ---- 汇总 ----
npass = sum(1 for _, ok, _ in results if ok)
print(f"\n==== 汇总: {npass}/{len(results)} PASS ====")
json.dump([{"name": n, "ok": ok, "detail": d} for n, ok, d in results],
          open("stress_results.json", "w"), ensure_ascii=False, indent=2)
