# -*- coding: utf-8 -*-
"""
用 adobe-ai-mcp 画一张「复古日落」海报（1200x750 pt），
全程走完整 MCP stdio 协议（和 Codex / Claude 客户端同一条路）。

跑法：python examples/make_artwork.py
产物：examples/artwork_retro_sunset.png（2x = 2400x1500）
      examples/artwork_retro_sunset.svg
      examples/artwork_retro_sunset.ai（可编辑源文件）
"""
import json
import math
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
SERVER = os.path.join(PROJ, "server.py")
OUT_PNG = os.path.join(HERE, "artwork_retro_sunset.png")
OUT_SVG = os.path.join(HERE, "artwork_retro_sunset.svg")
STATE = os.path.join(HERE, ".last_untitled.txt")

W, H = 1200, 750
CX, CY, R = 600, 430, 250          # 太阳
STRIPE_H = 5                       # 太阳镂空条纹的高度

SKY = [                            # 天空渐变色带（全宽横带，柔和过渡）
    (0, 130, "#FAF1E4"),
    (130, 260, "#F9EAD7"),
    (260, 390, "#F8E2C8"),
    (390, 520, "#F7DAB9"),
    (520, 640, "#F6D2AB"),
    (640, 750, "#F5CA9E"),
]


class Mcp:
    """极简 MCP 客户端：起 server.py，说 stdio JSON-RPC。"""

    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, SERVER], cwd=PROJ,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1,
        )
        self._id = 0
        self._request("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "artwork", "version": "1.0"},
        })
        self._notify("notifications/initialized")

    def _send(self, obj):
        self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def _recv(self):
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("server 提前退出：\n" + self.proc.stderr.read())
            line = line.strip()
            if line:
                return json.loads(line)

    def _notify(self, method):
        self._send({"jsonrpc": "2.0", "method": method})

    def _request(self, method, params=None):
        self._id += 1
        self._send({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}})
        return self._recv()

    def call(self, tool, **args):
        r = self._request("tools/call", {"name": tool, "arguments": args})
        res = r["result"]
        text = next((c["text"] for c in res["content"] if c["type"] == "text"), "")
        imgs = [c for c in res["content"] if c["type"] == "image"]
        if res.get("isError"):
            raise RuntimeError("%s 失败：%s" % (tool, text))
        return text, imgs

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=20)
        except Exception:
            pass


def sky_at(y):
    """取某个高度上的天空颜色（太阳镂空条纹用它，边缘可以无缝衔接）。"""
    for y0, y1, c in SKY:
        if y0 <= y < y1:
            return c
    return SKY[-1][2]


def main():
    mcp = Mcp()
    calls = 0

    def do(tool, **args):
        nonlocal calls
        calls += 1
        text, imgs = mcp.call(tool, **args)
        print("[%02d] %-16s %s" % (calls, tool, text.replace("\n", " ")[:110]))
        return text, imgs

    try:
        do("ai_status")
        # 收掉上一个烟测留下的文档，保持桌面干净（名字对不上就算了）
        for nm in ("未标题-1", "Untitled-1"):
            try:
                do("ai_close_document", name=nm)
                break
            except Exception:
                calls -= 1

        text, _ = do("ai_new_document", width=W, height=H, unit="pt", color_mode="RGB")
        docname = (json.loads(text) or {}).get("document") or ""
        with open(STATE, "w", encoding="utf-8") as f:
            f.write(docname)
        print("     文档：", docname)

        # 1) 米色底
        do("ai_add_rect", x=0, y=0, width=W, height=H, fill="#F7EAD7", name="bg")

        # 2) 天空渐变色带
        for i, (y0, y1, c) in enumerate(SKY):
            do("ai_add_rect", x=0, y=y0, width=W, height=y1 - y0, fill=c, name="sky_%d" % i)

        # 3) 太阳的外圈细环（复古海报的小心机）
        do("ai_add_ellipse", cx=CX, cy=CY, rx=283, ry=283,
           fill="none", stroke="#E2603F", stroke_width=3, opacity=60, name="ring1")
        do("ai_add_ellipse", cx=CX, cy=CY, rx=312, ry=312,
           fill="none", stroke="#E2603F", stroke_width=2, opacity=28, name="ring2")

        # 4) 实心落日 + 中部镂空横纹（条纹宽度按圆的弦长计算，边缘干净）
        do("ai_add_ellipse", cx=CX, cy=CY, rx=R, ry=R, fill="#E2603F", name="sun")
        n = 0
        for k in range(-7, 8):  # dy = -210 .. +210，步长 30
            dy = k * 30
            tol = abs(dy) + STRIPE_H / 2.0
            if tol >= R - 6:
                continue
            half_w = math.sqrt(R * R - tol * tol) - 1.0
            if half_w < 40:
                continue
            y = CY + dy - STRIPE_H / 2.0
            do("ai_add_rect", x=round(CX - half_w, 2), y=round(y, 2),
               width=round(2 * half_w, 2), height=STRIPE_H,
               fill=sky_at(y + STRIPE_H / 2.0), name="sun_stripe_%d" % n)
            n += 1
        print("     太阳镂空条纹：", n, "条")

        # 4) 海鸥
        do("ai_add_path", d="M 400 302 Q 410 287 421 298 Q 432 287 442 302",
           fill="none", stroke="#3A2450", stroke_width=4.5, name="bird1")
        do("ai_add_path", d="M 466 337 Q 475 325 485 334 Q 495 325 504 337",
           fill="none", stroke="#3A2450", stroke_width=4, name="bird2")
        do("ai_add_path", d="M 528 287 Q 536 277 545 285 Q 553 277 561 287",
           fill="none", stroke="#3A2450", stroke_width=3.5, name="bird3")

        # 5) 远山（雾紫）
        do("ai_add_path", name="ridge_far", fill="#8E6193",
           d=("M 0 620 C 110 560 210 552 330 600 C 420 636 490 570 600 560 "
              "C 700 551 770 610 870 600 C 960 591 1040 545 1120 570 "
              "C 1160 583 1185 592 1200 590 L 1200 750 L 0 750 Z"))

        # 6) 近山（深紫）
        do("ai_add_path", name="ridge_near", fill="#5A3B72",
           d=("M 0 690 C 80 640 160 615 260 640 C 350 662 400 622 480 630 "
              "C 560 638 600 690 690 675 C 770 662 830 618 920 632 "
              "C 1000 645 1060 680 1130 672 C 1170 667 1190 668 1200 670 "
              "L 1200 750 L 0 750 Z"))

        # 7) 水面 + 夕阳反光
        do("ai_add_rect", x=0, y=702, width=W, height=48, fill="#241535", name="water")
        do("ai_add_rect", x=410, y=708, width=380, height=9, corner_radius=4,
           fill="#F0985A", opacity=60, name="glint1")
        do("ai_add_rect", x=470, y=722, width=260, height=8, corner_radius=4,
           fill="#F6BE76", opacity=50, name="glint2")
        do("ai_add_rect", x=528, y=735, width=144, height=7, corner_radius=3,
           fill="#FAD98F", opacity=45, name="glint3")

        # 8) 落款
        do("ai_add_text", text="AMI · 2026.10", x=64, y=706, size=22,
           color="#F3E3CB", opacity=90, name="sig")

        # 9) 导出：PNG 2x（内嵌返回校验） + SVG
        text, imgs = do("ai_export", path=OUT_PNG, format="png", scale=2.0, preview=True)
        if imgs:
            import base64
            raw = base64.b64decode(imgs[0]["data"])
            same = len(raw) == os.path.getsize(OUT_PNG)
            print("     内嵌图校验：%s（%d 字节）" % ("一致" if same else "不一致！", len(raw)))
        do("ai_export", path=OUT_SVG, format="svg")

        # 10) 存一份可编辑的 .ai 源文件（也顺手把文档从 svg 关联里解放出来）
        do("ai_save", path=os.path.join(HERE, "artwork_retro_sunset.ai"))
        print("\n完成：%s\n      %s\n      %s" % (
            OUT_PNG, OUT_SVG, os.path.join(HERE, "artwork_retro_sunset.ai")))
    finally:
        mcp.close()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
