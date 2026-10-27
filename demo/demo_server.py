#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PhotoFlux AI (com.technosub.unlimitedaiimagen v81.0) — 第一轮 demo 网关
上游通道（逆向自 APK）：
  1. pollinations   文生图（免鉴权，已验证出真图）        /v1/images/generations
  2. bria-rmbg      抠图（HuggingFace Space gradio，免鉴权） /v1/images/remove-bg
  3. img2go         图像编辑任务（匿名用户 credit=16/日）    /v1/img2go/{user,credits}
  4. moderation     内容审核代理（上游 404，占位）
  5. writecream     生成（上游 404 已下线，占位）
  6. fireworks/unlim.store/runway 等 —— 密钥在 libphotoflux_security.so（需模拟器动态调起，第二轮）
"""
import base64, io, json, re, ssl, threading, time, uuid, hashlib
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

TASKS = {}          # demo 任务队列（异步模式）
TASKS_LOCK = threading.Lock()

# pollinations 尺寸表（逆向自 PollinationImageGenerationService.getImageDimensions）
RATIOS = {
    "1": (1024, 1024), "2": (1024, 1024), "3": (1024, 1024),
    "4": (1024, 1344), "5": (1344, 1024), "6": (896, 1120),
    "7": (1120, 896),  "8": (832, 1248),  "9": (1248, 832),
    "10": (1536, 640), "11": (640, 1536),
}

UPSTREAM_STATS = {"pollinations": [0,0,0.0], "bria": [0,0,0.0], "img2go": [0,0,0.0]}  # ok/fail/total_ms
STATS_LOCK = threading.Lock()

def note_upstream(ch, ok, ms):
    with STATS_LOCK:
        s = UPSTREAM_STATS[ch]
        s[0 if ok else 1] += 1
        s[2] += ms

def http_req(url, method="GET", data=None, headers=None, timeout=120):
    h = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/154.0.0.0 Safari/537.36"}
    if headers: h.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    t = time.time()
    r = urllib.request.urlopen(req, timeout=timeout, context=CTX)
    body = r.read()
    return r.status, body, (time.time() - t) * 1000

# ---------- 上游通道实现 ----------

def up_pollinations(prompt, ratio="1", model="flux", seed=None):
    w, hgt = RATIOS.get(str(ratio), (1024, 1024))
    seed = seed if seed is not None else int(time.time() * 1000) % 10**9
    url = (f"https://image.pollinations.ai/prompt/{urllib.parse.quote(prompt)}"
           f"?width={w}&height={hgt}&seed={seed}&model={model}&nologo=true")
    # Classic API 有全局并发限制（503 Retry-After 10s），做指数退避重试
    import urllib.error
    last = None
    for attempt in range(4):
        try:
            status, body, ms = http_req(url, timeout=120)
        except urllib.error.HTTPError as e:
            status, body, ms = e.code, e.read(), 0
        if status == 200 and body[:3] in (b"\xff\xd8\xff", b"\x89PNG"):
            note_upstream("pollinations", True, ms)
            return body, "image/jpeg", {"width": w, "height": hgt, "seed": seed, "model": model, "upstream_ms": round(ms)}
        last = status
        note_upstream("pollinations", False, ms)
        if status in (503, 402, 429):
            time.sleep(8 * (attempt + 1))
        else:
            break
    raise RuntimeError(f"pollinations HTTP {last} after retries")

def up_bria_remove_bg(img_bytes):
    """完整 gradio 三步链路（逆向自 BriaAIBackgroundRemovalService）:
       1) /gradio_api/upload  2) POST /gradio_api/call/image  3) GET /call/image/<event_id> SSE"""
    import urllib.error
    base = "https://briaai-bria-rmbg-2-0.hf.space/gradio_api"
    boundary = uuid.uuid4().hex
    mp = (f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="in.jpg"\r\n'
          f"Content-Type: image/jpeg\r\n\r\n").encode() + img_bytes + f"\r\n--{boundary}--\r\n".encode()
    status, body, ms1 = http_req(f"{base}/upload", "POST", mp,
        {"Content-Type": f"multipart/form-data; boundary={boundary}"}, timeout=60)
    if status != 200:
        raise RuntimeError(f"bria upload HTTP {status}")
    tmp = json.loads(body)[0]
    payload = json.dumps({"data": [{"path": tmp, "meta": {"_type": "gradio.FileData"}}]}).encode()
    status, body, ms2 = http_req(f"{base}/call/image", "POST", payload,
        {"Content-Type": "application/json", "User-Agent": "Python-urllib/3.14"}, timeout=30)
    if status != 200:
        raise RuntimeError(f"bria call HTTP {status}: {body[:100]}")
    ev = json.loads(body)["event_id"]
    t = time.time()
    r = urllib.request.urlopen(urllib.request.Request(f"{base}/call/image/{ev}", headers={"User-Agent": "okhttp/4.9.3"}),
                               timeout=180, context=CTX)
    # SSE 流式读取直到 complete/error 事件
    txt = ""
    t0 = time.time()
    while time.time() - t0 < 150:
        line = r.readline()
        if not line:
            break
        txt += line.decode("utf-8", "ignore")
        if "event: complete" in txt or "event: error" in txt:
            # complete 后还有 data 行，再读一点
            txt += r.readline().decode("utf-8", "ignore") + r.readline().decode("utf-8", "ignore")
            break
    r.close()
    m = re.search(r'"url": "(https://[^"]+no_bg_image[^"]*)"', txt)
    if not m:
        m = re.findall(r'"url": "(https://[^"]+)"', txt)
        if not m:
            # ZeroGPU 匿名配额耗尽时 gradio 返回 event:error data:null —— 属间歇性上游限制
            if "event: error" in txt:
                raise RuntimeError("bria ZeroGPU quota exhausted (intermittent upstream limit, retry later)")
            raise RuntimeError("bria SSE no result")
        url = m[-1]
    else:
        url = m.group(1)
    status, out, ms3 = http_req(url, timeout=60)
    note_upstream("bria", status == 200, ms1 + ms2 + ms3 + (time.time() - t) * 1000)
    if status != 200:
        raise RuntimeError(f"bria download HTTP {status}")
    return out, ms1 + ms2 + ms3

def up_img2go_user():
    status, body, ms = http_req("https://dragon.img2go.com/api/user", headers={
        "origin": "https://www.img2go.com", "referer": "https://www.img2go.com/",
        "x-requested-with": "XMLHttpRequest"}, timeout=20)
    note_upstream("img2go", status == 200, ms)
    return json.loads(body)

def up_img2go_credits():
    status, body, ms = http_req("https://dragon.img2go.com/api/user/credit-bank", headers={
        "origin": "https://www.img2go.com", "referer": "https://www.img2go.com/",
        "x-requested-with": "XMLHttpRequest"}, timeout=20)
    note_upstream("img2go", status == 200, ms)
    return json.loads(body)

# ---------- HTTP 服务 ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "PhotoFluxDemo/1.0"

    def log_message(self, fmt, *a):
        print(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} {fmt % a}")

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode())

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n == 0: return {}
        try: return json.loads(self.rfile.read(n))
        except Exception: return {}

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/health":
            return self._json(200, {"ok": True, "service": "photoflux-demo", "ts": time.time()})
        if p == "/" or p == "/demo.html":
            return self._send(200, open("demo.html", "rb").read(), "text/html; charset=utf-8")
        if p == "/v1/models":
            return self._json(200, {"models": [
                {"id": "flux", "channel": "pollinations", "auth": "none"},
                {"id": "flux-realism", "channel": "pollinations", "auth": "none"},
                {"id": "bria-rmbg-2.0", "channel": "huggingface-space", "auth": "none"},
                {"id": "img2go-edit", "channel": "img2go", "auth": "anonymous-credit"},
            ]})
        if p == "/v1/stats":
            with STATS_LOCK:
                st = {k: {"ok": v[0], "fail": v[1], "avg_ms": round(v[2] / max(1, v[0] + v[1]))} for k, v in UPSTREAM_STATS.items()}
            with TASKS_LOCK:
                st["tasks"] = len(TASKS)
            return self._json(200, st)
        if p == "/v1/img2go/user":
            try: return self._json(200, up_img2go_user())
            except Exception as e: return self._json(502, {"error": str(e)})
        if p == "/v1/img2go/credits":
            try: return self._json(200, up_img2go_credits())
            except Exception as e: return self._json(502, {"error": str(e)})
        if p.startswith("/v1/tasks/"):
            tid = p.rsplit("/", 1)[-1]
            with TASKS_LOCK:
                t = TASKS.get(tid)
                if not t: return self._json(404, {"error": "task not found"})
                t = dict(t)
                if t["status"] == "done":
                    t["image_url"] = f"/v1/tasks/{tid}/image"
            return self._json(200, t)
        if p.startswith("/v1/tasks/") and p.endswith("/image"):
            tid = p.split("/")[3]
            with TASKS_LOCK:
                t = TASKS.get(tid)
            if not t or t["status"] != "done":
                return self._json(404, {"error": "not ready"})
            return self._send(200, t["image"], t.get("ctype", "image/jpeg"))
        return self._json(404, {"error": "not found", "path": p})

    def do_POST(self):
        p = self.path.split("?")[0]
        if p == "/v1/images/generations":
            req = self._read_json()
            prompt = (req.get("prompt") or "").strip()
            if not prompt:
                return self._json(400, {"error": "prompt required"})
            try:
                img, ctype, meta = up_pollinations(prompt, req.get("ratio", "1"), req.get("model", "flux"), req.get("seed"))
            except Exception as e:
                return self._json(502, {"error": f"upstream failed: {e}"})
            b64 = base64.b64encode(img).decode()
            return self._json(200, {"created": int(time.time()), "data": [{
                "b64_json": b64, "size": f"{meta['width']}x{meta['height']}",
                "seed": meta["seed"], "model": meta["model"], "upstream_ms": meta["upstream_ms"],
                "bytes": len(img)}]})
        if p == "/v1/images/remove-bg":
            body = self._read_body()
            if not body:
                return self._json(400, {"error": "image body required"})
            try:
                out, ms = up_bria_remove_bg(body)
            except Exception as e:
                return self._json(502, {"error": f"upstream failed: {e}"})
            return self._json(200, {"image_b64": base64.b64encode(out).decode(), "upstream_ms": round(ms), "bytes": len(out)})
        if p == "/v1/images/async":          # demo 任务队列（模拟 APK 内 Fireworks 异步链路）
            req = self._read_json()
            prompt = (req.get("prompt") or "").strip()
            if not prompt:
                return self._json(400, {"error": "prompt required"})
            tid = "task-" + uuid.uuid4().hex[:12]
            with TASKS_LOCK:
                TASKS[tid] = {"id": tid, "status": "queued", "created": time.time()}
            def worker():
                with TASKS_LOCK: TASKS[tid]["status"] = "running"
                try:
                    img, ctype, meta = up_pollinations(prompt, req.get("ratio", "1"), req.get("model", "flux"))
                    with TASKS_LOCK:
                        TASKS[tid].update(status="done", image=img, ctype=ctype, meta=meta, finished=time.time())
                except Exception as e:
                    with TASKS_LOCK:
                        TASKS[tid].update(status="failed", error=str(e), finished=time.time())
            threading.Thread(target=worker, daemon=True).start()
            return self._json(202, {"id": tid, "status": "queued"})
        return self._json(404, {"error": "not found", "path": p})

if __name__ == "__main__":
    import urllib.parse  # noqa
    port = 8921
    print(f"PhotoFlux demo gateway on http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
