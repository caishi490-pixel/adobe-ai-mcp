# -*- coding: utf-8 -*-
"""
adobe-ai-mcp · Illustrator COM 桥（零依赖）

通过系统自带的 Windows PowerShell 连接 Adobe Illustrator，
把 ExtendScript 送进 app.DoJavaScript 执行，再把 JSON 结果带回来。
不依赖 pywin32，任何装了 Python 3.8+ 的 Windows 都能跑。

工作原理：
  1. 把 JSX 脚本写到临时文件（UTF-8）
  2. 调用 scripts/com_bridge.ps1：GetActiveObject / CreateInstance 连上 Illustrator
  3. DoJavaScript(jsx) 执行，结果写回临时文件
  4. Python 读回结果并解析 JSON
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PS1_PATH = os.path.join(HERE, "scripts", "com_bridge.ps1")
POWERSHELL = os.environ.get("AMI_AI_POWERSHELL", "powershell.exe")
DEFAULT_TIMEOUT = int(os.environ.get("AMI_AI_TIMEOUT", "300"))


class BridgeError(RuntimeError):
    """桥接层错误（PowerShell / COM / 脚本执行失败等）。"""


class AppNotRunning(BridgeError):
    """Illustrator 没在运行，且本次调用不允许自动启动。"""


class AppNotInstalled(BridgeError):
    """这台机器上没有装 Adobe Illustrator。"""


# ---------------------------------------------------------------------------
# JSX 前置库：JSON 序列化 + 颜色 + 样式 + 图层 + SVG 路径解析
# （ExtendScript 是 ES3，没有 JSON.stringify，所以自带一个小序列化器）
# ---------------------------------------------------------------------------
PREAMBLE = r"""
var __AMI = (function () {

  // 注意：in 是 ES3 保留字，键必须带引号
  var UNIT = { "pt": 1, "px": 1, "mm": 72 / 25.4, "cm": 72 / 2.54, "in": 72 };

  function toPt(v, unit) {
    return Number(v) * (UNIT[String(unit || "pt").toLowerCase()] || 1);
  }

  function esc(s) {
    var out = "", i, ch, code;
    s = String(s);
    for (i = 0; i < s.length; i++) {
      ch = s.charAt(i);
      code = s.charCodeAt(i);
      if (ch === '"') { out += '\\"'; }
      else if (ch === '\\') { out += '\\\\'; }
      else if (ch === '\n') { out += '\\n'; }
      else if (ch === '\r') { out += '\\r'; }
      else if (ch === '\t') { out += '\\t'; }
      else if (code < 32) { out += '\\u' + ('000' + code.toString(16)).slice(-4); }
      else { out += ch; }
    }
    return out;
  }

  function ser(v, depth) {
    depth = depth || 0;
    if (depth > 10) { return '"..."'; }
    if (v === null || v === undefined) { return 'null'; }
    var t = typeof v, parts = [], i, k;
    if (t === 'number') { return isFinite(v) ? String(v) : 'null'; }
    if (t === 'boolean') { return v ? 'true' : 'false'; }
    if (t === 'string') { return '"' + esc(v) + '"'; }
    if (t === 'function') { return 'null'; }
    if (t === 'object' && v.length !== undefined && v.join !== undefined) {
      for (i = 0; i < v.length; i++) { parts.push(ser(v[i], depth + 1)); }
      return '[' + parts.join(',') + ']';
    }
    if (t === 'object') {
      for (k in v) {
        if (Object.prototype.hasOwnProperty.call(v, k)) {
          parts.push('"' + esc(String(k)) + '":' + ser(v[k], depth + 1));
        }
      }
      return '{' + parts.join(',') + '}';
    }
    return '"' + esc(String(v)) + '"';
  }

  function hexToRGB(c) {
    c = String(c).replace(/^\s*#/, '');
    if (c.length === 3) {
      c = c.charAt(0) + c.charAt(0) + c.charAt(1) + c.charAt(1) + c.charAt(2) + c.charAt(2);
    }
    var r = parseInt(c.substring(0, 2), 16);
    var g = parseInt(c.substring(2, 4), 16);
    var b = parseInt(c.substring(4, 6), 16);
    return { r: isNaN(r) ? 0 : r, g: isNaN(g) ? 0 : g, b: isNaN(b) ? 0 : b };
  }

  function color(spec) {
    if (spec === null || spec === undefined) { return null; }
    if (typeof spec === 'string') {
      var s = spec.replace(/^\s+|\s+$/g, '').toLowerCase();
      if (s === '' || s === 'none' || s === 'transparent') { return null; }
      if (s === 'black') { s = '#000000'; }
      if (s === 'white') { s = '#ffffff'; }
      var h = hexToRGB(s), c1 = new RGBColor();
      c1.red = h.r; c1.green = h.g; c1.blue = h.b;
      return c1;
    }
    if (typeof spec === 'object') {
      if (spec.hex !== undefined) { return color(spec.hex); }
      if (spec.c !== undefined) {
        var cm = new CMYKColor();
        cm.cyan = Number(spec.c); cm.magenta = Number(spec.m);
        cm.yellow = Number(spec.y); cm.black = Number(spec.k);
        return cm;
      }
      if (spec.gray !== undefined) {
        var gc = new GrayColor(); gc.gray = Number(spec.gray); return gc;
      }
      var c2 = new RGBColor();
      c2.red = Number(spec.r); c2.green = Number(spec.g); c2.blue = Number(spec.b);
      return c2;
    }
    return null;
  }

  function style(item, st) {
    st = st || {};
    if (st.fill !== undefined) {
      var fc = color(st.fill);
      if (fc === null) { item.filled = false; }
      else { item.filled = true; item.fillColor = fc; }
    }
    if (st.stroke !== undefined) {
      var sc = color(st.stroke);
      if (sc === null) { item.stroked = false; }
      else {
        item.stroked = true;
        item.strokeColor = sc;
        item.strokeWidth = (st.stroke_width === undefined) ? 1 : Number(st.stroke_width);
      }
    } else if (st.stroke_width !== undefined && item.stroked) {
      item.strokeWidth = Number(st.stroke_width);
    }
    if (st.opacity !== undefined) { item.opacity = Number(st.opacity); }
    return item;
  }

  function doc() {
    if (app.documents.length === 0) {
      throw new Error('没有打开的文档：请先调用 ai_new_document 新建，或 ai_open 打开现有文件');
    }
    return app.activeDocument;
  }

  function itemByName(name) {
    var d = doc();
    try {
      return d.pageItems.getByName(String(name));
    } catch (e) {
      throw new Error('找不到名为 "' + name + '" 的对象');
    }
  }

  // Illustrator 脚本原生坐标系：原点在画板左下角、y 向上。
  // 对外统一用"画板左上角原点、y 向下"（和 CSS/SVG 直觉一致），在这里换算。
  function ab(d) { return d.artboards[0].artboardRect; }  // [left, top, right, bottom]
  function X(d, x) { return ab(d)[0] + Number(x); }
  function Y(d, y) { return ab(d)[1] - Number(y); }

  function boundsOf(d, it) {
    var b = null;
    try { b = it.geometricBounds; } catch (e) { b = null; }
    if (!b) { return null; }
    var L = ab(d)[0], T = ab(d)[1];
    return {
      left: b[0] - L,
      top: T - b[1],
      right: b[2] - L,
      bottom: T - b[3],
      width: b[2] - b[0],
      height: b[1] - b[3]
    };
  }

  function layer(d, name, createIfMissing) {
    if (!name) { return d.activeLayer; }
    var i;
    for (i = 0; i < d.layers.length; i++) {
      if (d.layers[i].name === String(name)) { return d.layers[i]; }
    }
    if (createIfMissing) {
      var L = d.layers.add();
      L.name = String(name);
      return L;
    }
    throw new Error('图层不存在: ' + name);
  }

  function withLayer(d, name, fn) {
    var prev = d.activeLayer;
    var target = layer(d, name, true);
    d.activeLayer = target;
    try {
      return fn();
    } finally {
      d.activeLayer = prev;
    }
  }

  function parsePathData(str) {
    var re = /([MmLlHhVvCcSsQqTtAaZz])|(-?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?)/g;
    var m, toks = [];
    while ((m = re.exec(str)) !== null) {
      toks.push(m[1] !== undefined ? m[1] : parseFloat(m[2]));
    }
    var i = 0, cmd = null, cur = null, subs = [], warnings = [];
    var x = 0, y = 0, sx = 0, sy = 0, prevType = '', prevC = null, prevQ = null;

    function num() {
      if (i >= toks.length || typeof toks[i] === 'string') {
        throw new Error('路径数据不完整（位置 ' + i + '）');
      }
      var v = toks[i]; i += 1;
      return v;
    }
    function node(px, py) {
      return { x: px, y: py, inx: null, iny: null, outx: null, outy: null };
    }
    function ensureSub() {
      if (!cur) {
        cur = { nodes: [node(0, 0)], closed: false };
        subs.push(cur);
      }
    }
    function lineTo(nx, ny) {
      ensureSub();
      cur.nodes.push(node(nx, ny));
    }
    function curveTo(c1x, c1y, c2x, c2y, nx, ny) {
      ensureSub();
      var last = cur.nodes[cur.nodes.length - 1];
      last.outx = c1x; last.outy = c1y;
      var n = node(nx, ny);
      n.inx = c2x; n.iny = c2y;
      cur.nodes.push(n);
    }
    function closeSub() {
      if (!cur) { return; }
      cur.closed = true;
      var first = cur.nodes[0], last = cur.nodes[cur.nodes.length - 1];
      if (cur.nodes.length > 1 && Math.abs(first.x - last.x) < 1e-6 && Math.abs(first.y - last.y) < 1e-6) {
        first.inx = last.inx; first.iny = last.iny;
        cur.nodes.pop();
      }
      cur = null;
    }

    while (i < toks.length) {
      if (typeof toks[i] === 'string') {
        cmd = toks[i]; i += 1;
      } else {
        if (!cmd) { throw new Error('SVG 路径必须以命令字母开头（M/m）'); }
        if (cmd === 'Z' || cmd === 'z') { throw new Error('路径数据错误：Z 后面不能直接跟数字'); }
        if (cmd === 'M') { cmd = 'L'; } else if (cmd === 'm') { cmd = 'l'; }
      }
      var rel = (cmd === cmd.toLowerCase());
      var C = cmd.toUpperCase();
      if (C === 'M') {
        var mx = num(), my = num();
        if (rel) { mx += x; my += y; }
        x = mx; y = my; sx = x; sy = y;
        cur = { nodes: [node(x, y)], closed: false };
        subs.push(cur);
        prevType = 'M'; prevC = null; prevQ = null;
      } else if (C === 'L') {
        var lx = num(), ly = num();
        if (rel) { lx += x; ly += y; }
        lineTo(lx, ly); x = lx; y = ly;
        prevType = 'L'; prevC = null; prevQ = null;
      } else if (C === 'H') {
        var hx = num();
        if (rel) { hx += x; }
        lineTo(hx, y); x = hx;
        prevType = 'L'; prevC = null; prevQ = null;
      } else if (C === 'V') {
        var vy = num();
        if (rel) { vy += y; }
        lineTo(x, vy); y = vy;
        prevType = 'L'; prevC = null; prevQ = null;
      } else if (C === 'C') {
        var c1x = num(), c1y = num(), c2x = num(), c2y = num(), ex = num(), ey = num();
        if (rel) { c1x += x; c1y += y; c2x += x; c2y += y; ex += x; ey += y; }
        curveTo(c1x, c1y, c2x, c2y, ex, ey);
        prevC = [c2x, c2y];
        prevType = 'C'; prevQ = null;
        x = ex; y = ey;
      } else if (C === 'S') {
        var s2x = num(), s2y = num(), sex = num(), sey = num();
        if (rel) { s2x += x; s2y += y; sex += x; sey += y; }
        var r1x = x, r1y = y;
        if (prevType === 'C' || prevType === 'S') { r1x = 2 * x - prevC[0]; r1y = 2 * y - prevC[1]; }
        curveTo(r1x, r1y, s2x, s2y, sex, sey);
        prevC = [s2x, s2y];
        prevType = 'S'; prevQ = null;
        x = sex; y = sey;
      } else if (C === 'Q') {
        var qx = num(), qy = num(), qex = num(), qey = num();
        if (rel) { qx += x; qy += y; qex += x; qey += y; }
        var q1x = x + (qx - x) * 2 / 3, q1y = y + (qy - y) * 2 / 3;
        var q2x = qex + (qx - qex) * 2 / 3, q2y = qey + (qy - qey) * 2 / 3;
        curveTo(q1x, q1y, q2x, q2y, qex, qey);
        prevQ = [qx, qy];
        prevType = 'Q'; prevC = null;
        x = qex; y = qey;
      } else if (C === 'T') {
        var tex = num(), tey = num();
        if (rel) { tex += x; tey += y; }
        var tqx = x, tqy = y;
        if (prevType === 'Q' || prevType === 'T') { tqx = 2 * x - prevQ[0]; tqy = 2 * y - prevQ[1]; }
        var t1x = x + (tqx - x) * 2 / 3, t1y = y + (tqy - y) * 2 / 3;
        var t2x = tex + (tqx - tex) * 2 / 3, t2y = tey + (tqy - tey) * 2 / 3;
        curveTo(t1x, t1y, t2x, t2y, tex, tey);
        prevQ = [tqx, tqy];
        prevType = 'T'; prevC = null;
        x = tex; y = tey;
      } else if (C === 'A') {
        var arx = num(), ary = num(), arot = num(), alaf = num(), asf = num(), aex = num(), aey = num();
        if (rel) { aex += x; aey += y; }
        lineTo(aex, aey);
        x = aex; y = aey;
        prevType = 'A'; prevC = null; prevQ = null;
        warnings.push('圆弧命令 A 暂不支持，已按直线连接（可改用 ai_add_ellipse）');
      } else if (C === 'Z') {
        closeSub();
        prevType = 'Z'; prevC = null; prevQ = null;
      } else {
        throw new Error('不支持的路径命令: ' + cmd);
      }
    }
    return { subs: subs, warnings: warnings };
  }

  function drawPath(d, str, layerName, st, name) {
    var parsed = parsePathData(str);
    var made = [], k, j, sub, nodes, pi, p, n, smooth;
    var L = ab(d)[0], T = ab(d)[1];
    for (k = 0; k < parsed.subs.length; k++) {
      sub = parsed.subs[k];
      nodes = sub.nodes;
      if (!nodes || nodes.length < 2) { continue; }
      pi = withLayer(d, layerName, function () { return d.pathItems.add(); });
      for (j = 0; j < nodes.length; j++) {
        p = pi.pathPoints.add();
        p.anchor = [L + nodes[j].x, T - nodes[j].y];
      }
      for (j = 0; j < nodes.length; j++) {
        n = nodes[j];
        p = pi.pathPoints[j];
        if (n.inx !== null) { p.leftDirection = [L + n.inx, T - n.iny]; }
        else { p.leftDirection = [L + n.x, T - n.y]; }
        if (n.outx !== null) { p.rightDirection = [L + n.outx, T - n.outy]; }
        else { p.rightDirection = [L + n.x, T - n.y]; }
        smooth = false;
        if (n.inx !== null && n.outx !== null) {
          var d1x = n.x - n.inx, d1y = n.y - n.iny;
          var d2x = n.outx - n.x, d2y = n.outy - n.y;
          var l1 = Math.sqrt(d1x * d1x + d1y * d1y);
          var l2 = Math.sqrt(d2x * d2x + d2y * d2y);
          if (l1 < 1e-6 || l2 < 1e-6 || Math.abs(d1x * d2y - d1y * d2x) < 1e-4 * l1 * l2) { smooth = true; }
        }
        p.pointType = smooth ? PointType.SMOOTH : PointType.CORNER;
      }
      pi.closed = !!sub.closed;
      style(pi, st);
      if (name) {
        pi.name = (parsed.subs.length > 1) ? (String(name) + ' ' + (k + 1)) : String(name);
      }
      made.push(pi);
    }
    var bb = null, i, b;
    for (i = 0; i < made.length; i++) {
      b = boundsOf(d, made[i]);
      if (!b) { continue; }
      if (!bb) {
        bb = { left: b.left, top: b.top, right: b.right, bottom: b.bottom };
      } else {
        bb.left = Math.min(bb.left, b.left); bb.top = Math.min(bb.top, b.top);
        bb.right = Math.max(bb.right, b.right); bb.bottom = Math.max(bb.bottom, b.bottom);
      }
    }
    if (bb) { bb.width = bb.right - bb.left; bb.height = bb.bottom - bb.top; }
    return { count: made.length, bounds: bb, warnings: parsed.warnings };
  }

  return {
    s: ser,
    toPt: toPt,
    color: color,
    style: style,
    doc: doc,
    itemByName: itemByName,
    bounds: boundsOf,
    ab: ab,
    X: X,
    Y: Y,
    layer: layer,
    withLayer: withLayer,
    drawPath: drawPath,
    parsePathData: parsePathData
  };
})();
"""


def run_jsx(body: str, auto_launch: bool = True, timeout: int | None = None):
    """
    执行一段 JSX（脚本里可以自由用 return），返回 (解析后的字典, 元信息字典)。

    auto_launch=False 时，如果 Illustrator 没在运行会抛 AppNotRunning。
    """
    timeout = timeout or DEFAULT_TIMEOUT
    jsx = PREAMBLE + "\n(function () {\n" + body + "\n})();\n"
    tmpdir = tempfile.mkdtemp(prefix="ami_ai_")
    jsx_path = os.path.join(tmpdir, "job.jsx")
    out_path = os.path.join(tmpdir, "out.json")
    try:
        with open(jsx_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(jsx)
        cmd = [
            POWERSHELL, "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-File", PS1_PATH,
            "-JsxPath", jsx_path,
            "-OutPath", out_path,
        ]
        if auto_launch:
            cmd.append("-Launch")
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise BridgeError(
                "调用 Illustrator 超时（%ss）。如果 Illustrator 正在启动或弹窗，等它就绪后再试。" % timeout
            )
        if not os.path.exists(out_path):
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()
            if not err:
                err = (proc.stdout or b"").decode("utf-8", "replace").strip()
            raise BridgeError("PowerShell 桥执行失败（无结果文件）：%s" % (err[:600] or "无输出"))
        with open(out_path, "r", encoding="utf-8") as f:
            payload = json.loads(f.read())
        if not payload.get("ok"):
            msg = payload.get("error") or "未知错误"
            code = payload.get("code")
            if code == "not_running":
                raise AppNotRunning(msg)
            if code == "not_installed":
                raise AppNotInstalled(msg)
            raise BridgeError(msg)
        raw = (payload.get("result") or "").strip()
        if not raw:
            raise BridgeError("JSX 没有返回值（脚本可能没走到 return）。")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise BridgeError("JSX 返回值不是 JSON：%s" % raw[:300])
        meta = {
            "was_running": bool(payload.get("wasRunning")),
            "retries": payload.get("retries", 0),
        }
        return data, meta
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
