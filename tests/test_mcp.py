# -*- coding: utf-8 -*-
"""
烟测（走完整 MCP stdio 协议，和 Codex / Claude 客户端同一条路）：
启动 server.py → initialize → tools/list → tools/call 画一张小图并导出，
校验导出的 PNG 能通过 image content 返回。

跑法：在工程根目录执行  python tests/test_mcp.py
"""
import base64
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
SERVER = os.path.join(PROJ, "server.py")
OUT = os.path.join(PROJ, "examples", "mcp_demo.png")

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def main():
    proc = subprocess.Popen(
        [sys.executable, SERVER],
        cwd=PROJ,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", bufsize=1,
    )

    def send(obj):
        proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    def recv():
        while True:
            line = proc.stdout.readline()
            if not line:
                err = proc.stderr.read()
                raise RuntimeError("server 提前退出。stderr:\n" + err)
            line = line.strip()
            if line:
                return json.loads(line)

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                     "clientInfo": {"name": "smoke", "version": "1.0"}}})
    r = recv()
    assert r["result"]["serverInfo"]["name"] == "adobe-ai-mcp"
    print("initialize ok:", r["result"]["serverInfo"], "protocol", r["result"]["protocolVersion"])

    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    r = recv()
    tools = r["result"]["tools"]
    print("tools (%d):" % len(tools), ", ".join(t["name"] for t in tools))
    assert len(tools) >= 15

    next_id = [10]

    def call(name, args, expect_image=False):
        next_id[0] += 1
        send({"jsonrpc": "2.0", "id": next_id[0], "method": "tools/call",
              "params": {"name": name, "arguments": args}})
        r = recv()
        res = r["result"]
        txt = next(c["text"] for c in res["content"] if c["type"] == "text")
        imgs = [c for c in res["content"] if c["type"] == "image"]
        print("[%s] %-16s %s%s" % ("ERR" if res.get("isError") else "OK ", name,
                                   txt.replace("\n", " ")[:130],
                                   " [image]" if imgs else ""))
        assert not res.get("isError"), txt
        if expect_image:
            assert imgs, "预期返回图片内容但没有"
        return txt, imgs

    call("ai_status", {})
    call("ai_new_document", {"width": 900, "height": 600})
    call("ai_add_rect", {"x": 0, "y": 0, "width": 900, "height": 600, "fill": "#111827"})
    call("ai_add_ellipse", {"cx": 450, "cy": 280, "rx": 180, "ry": 180,
                            "fill": "#38BDF8", "opacity": 85})
    call("ai_add_text", {"text": "MCP 通了", "x": 260, "y": 440, "size": 64, "color": "#F9FAFB"})
    _, imgs = call("ai_export", {"path": OUT, "format": "png", "scale": 1.0}, expect_image=True)

    raw = base64.b64decode(imgs[0]["data"])
    copy_path = os.path.join(PROJ, "examples", "mcp_demo_from_protocol.png")
    with open(copy_path, "wb") as f:
        f.write(raw)
    assert os.path.getsize(OUT) == len(raw), "导出文件与协议返回的图片大小不一致"
    call("ai_close_document", {})

    proc.stdin.close()
    proc.wait(timeout=20)
    print("\nPASS  MCP 全链路正常。导出图:", OUT)


if __name__ == "__main__":
    main()
