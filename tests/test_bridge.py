# -*- coding: utf-8 -*-
"""
烟测（直接走桥接层，不经 MCP 协议）：
让 Illustrator 画一张演示图，导出 examples/demo.png 并核对对象外框。

跑法：在工程根目录执行  python tests/test_bridge.py
第一次会启动 Illustrator（约 1 分钟），之后每次几秒。
"""
import os
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
sys.path.insert(0, PROJ)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import server  # noqa: E402

OUT = os.path.join(PROJ, "examples", "demo.png")
STATE = os.path.join(PROJ, "examples", ".last_untitled.txt")


def call(tool, verbose=False, **args):
    content, is_err = server.call_tool(tool, args)
    txt = ""
    for c in content:
        if c.get("type") == "text":
            txt = c["text"]
            break
    shown = txt if verbose else txt.replace("\n", " ")[:150]
    print("[%s] %-16s %s" % ("ERR" if is_err else "OK ", tool, shown))
    if is_err:
        raise SystemExit("烟测失败于: " + tool)
    return txt


def main():
    call("ai_status")
    call("ai_launch")
    # 若上次烟测留下了未标题文档，先关掉那一个（名字对不上就不动）
    if os.path.exists(STATE):
        last = open(STATE, encoding="utf-8").read().strip()
        if last:
            server.call_tool("ai_close_document", {"name": last})
    txt = call("ai_new_document", width=1200, height=750, unit="pt", color_mode="RGB")
    docname = (json.loads(txt) or {}).get("document") or ""
    with open(STATE, "w", encoding="utf-8") as f:
        f.write(docname)
    print("本图所在文档：", docname)

    call("ai_add_rect", x=0, y=0, width=1200, height=750, fill="#F6F1E7", name="bg")
    print("--- 核对 bg 外框（应为 left=0, top=0, right=1200, bottom=750） ---")
    call("ai_list_items", verbose=True)

    call("ai_add_ellipse", cx=950, cy=210, rx=95, ry=95, fill="#F2C14E", name="sun")
    call("ai_add_path", name="hills", fill="#9DB4A0",
         d="M 0 750 L 0 520 Q 220 360 440 520 Q 660 430 880 560 T 1200 610 L 1200 750 Z")
    call("ai_add_path", name="wave", fill="none", stroke="#2F4858", stroke_width=6,
         d="M 80 640 C 200 560 320 720 440 640 S 680 720 800 640 S 1040 720 1160 640")
    call("ai_add_path", name="tri", fill="#E27D60",
         d="M 560 620 L 640 470 L 720 620 Z")
    call("ai_add_star", cx=250, cy=185, outer_radius=58, inner_radius=24, points=5,
         fill="#E27D60", name="star")
    call("ai_add_text", text="阿米 x Illustrator", x=80, y=80, size=46,
         color="#2F4858", name="title")

    call("ai_set_style", name="star", fill="#C0392B")

    call("ai_export", path=OUT, format="png", scale=1.0, preview=False)

    # 机制抽查：平移方向 / 缩放锚点 / 旋转方向（都在探针对象上做，完了删掉）
    print("\n--- 机制抽查（探针对象，不影响成品） ---")
    call("ai_add_rect", x=100, y=100, width=100, height=50, fill="#FFFFFF", name="probe")
    call("ai_transform", name="probe", dx=10, dy=20, verbose=True)
    call("ai_transform", name="probe", scale_x=2, verbose=True)
    call("ai_add_path", name="probe2", fill="#000000", d="M 200 300 L 300 300 L 300 350 Z")
    print("probe2 anchor 旋转前:", call("ai_run_script", code='return String(__AMI.itemByName("probe2").pathPoints[0].anchor);'))
    call("ai_transform", name="probe2", rotate=90, verbose=True)
    print("probe2 anchor 旋转后:", call("ai_run_script", code='return String(__AMI.itemByName("probe2").pathPoints[0].anchor);'))
    call("ai_delete", name="probe")
    call("ai_delete", name="probe2")
    call("ai_add_line", x1=50, y1=50, x2=150, y2=100, stroke="#FF00FF", stroke_width=2, name="probe3", verbose=True)
    call("ai_delete", name="probe3")
    call("ai_add_polygon", cx=600, cy=550, radius=60, sides=6, fill="#333333", name="probe4", verbose=True)
    call("ai_delete", name="probe4")

    print("\n完成。图在这里：", OUT)
    print("（文档 %s 会留在 Illustrator 里不关，方便肉眼看；下次跑本测试会先把它关掉）" % docname)


if __name__ == "__main__":
    main()
