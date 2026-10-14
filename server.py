# -*- coding: utf-8 -*-
"""
adobe-ai-mcp · Adobe Illustrator MCP server（零第三方依赖）

让 LLM 直接操作 Adobe Illustrator 画矢量图：
新建文档、画矩形/圆/多边形/星形/直线/SVG 路径、加文字、改样式、
变换、删除、打开/保存/导出（PNG/SVG/PDF/AI）、还能跑任意 ExtendScript。

实现：MCP stdio（换行分隔 JSON-RPC 2.0）+ PowerShell COM 桥（见 ai_bridge.py）。
只依赖 Python 标准库，和系统自带的 Windows PowerShell。

作者：阿米（Codex）
"""
from __future__ import annotations

import base64
import json
import os
import sys
import traceback
from string import Template

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import ai_bridge  # noqa: E402

SERVER_NAME = "adobe-ai-mcp"
SERVER_VERSION = "1.0.0"
DEFAULT_PROTOCOL = "2025-06-18"
KNOWN_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}
PREVIEW_MAX_BYTES = 6 * 1024 * 1024


class ToolError(RuntimeError):
    """工具层的可预期错误（参数问题、对象找不到等）。"""


def log(msg: str) -> None:
    print("[adobe-ai-mcp] " + str(msg), file=sys.stderr, flush=True)


def send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def J(v) -> str:
    """把 Python 值转成安全的 JS 字面量（ensure_ascii 避免编码问题）。"""
    return json.dumps(v, ensure_ascii=True)


def text(s: str) -> dict:
    return {"type": "text", "text": s}


def req(a: dict, *names: str) -> None:
    missing = [n for n in names if a.get(n) is None]
    if missing:
        raise ToolError("缺少参数：" + "、".join(missing))


def num(a: dict, key: str, default=None) -> float:
    v = a.get(key, default)
    if v is None:
        raise ToolError("缺少参数：" + key)
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ToolError("参数 %s 必须是数字，收到：%r" % (key, v))


def _style_full(a: dict, default_fill: str = "#000000") -> dict:
    st = {
        "fill": a.get("fill", default_fill),
        "stroke": a.get("stroke", "none"),
        "stroke_width": a.get("stroke_width", 1),
    }
    if a.get("opacity") is not None:
        st["opacity"] = a["opacity"]
    return st


def _style_partial(a: dict) -> dict:
    st = {}
    for k in ("fill", "stroke", "stroke_width", "opacity"):
        if a.get(k) is not None:
            st[k] = a[k]
    if not st:
        raise ToolError("请至少提供一个要修改的样式字段（fill / stroke / stroke_width / opacity）")
    return st


def jsx_result(body: str) -> list:
    data, _meta = ai_bridge.run_jsx(body)
    if isinstance(data, dict) and data.get("ok") is False:
        raise ToolError("Illustrator 报告失败：" + str(data.get("error") or data))
    return [text(json.dumps(data, ensure_ascii=False, indent=1))]


# ---------------------------------------------------------------------------
# JSX 模板
# ---------------------------------------------------------------------------

STATUS_TMPL = Template(r"""
var ver = "", appname = "";
try { ver = String(app.version); } catch (e) { ver = ""; }
try { appname = String(app.name); } catch (e) { appname = ""; }
var active = null;
if (app.documents.length > 0) { try { active = String(app.activeDocument.name); } catch (e2) { active = null; } }
return __AMI.s({
  ok: true,
  appName: appname,
  version: ver,
  documents: app.documents.length,
  activeDocument: active
});
""")

NEW_DOC_TMPL = Template(r"""
var mode = $color_mode;
var cs = (mode === 'CMYK') ? DocumentColorSpace.CMYK : DocumentColorSpace.RGB;
var d = app.documents.add(cs, $w, $h);
return __AMI.s({ ok: true, document: String(d.name), width: $w, height: $h, unit: 'pt', colorMode: mode });
""")

RECT_TMPL = Template(r"""
var d = __AMI.doc();
var st = $st;
var top = __AMI.Y(d, $y);
var left = __AMI.X(d, $x);
var it = __AMI.withLayer(d, $layer, function () {
  var r = $r;
  if (r > 0) { return d.pathItems.roundedRectangle(top, left, $w, $h, r, r); }
  return d.pathItems.rectangle(top, left, $w, $h);
});
__AMI.style(it, st);
var nm = $name;
if (nm) { it.name = String(nm); }
return __AMI.s({ ok: true, name: String(it.name), typename: String(it.typename), bounds: __AMI.bounds(d, it) });
""")

ELLIPSE_TMPL = Template(r"""
var d = __AMI.doc();
var st = $st;
var cx = __AMI.X(d, $cx);
var cy = __AMI.Y(d, $cy);
var it = __AMI.withLayer(d, $layer, function () {
  return d.pathItems.ellipse(cy + $ry, cx - $rx, $rx * 2, $ry * 2);
});
__AMI.style(it, st);
var nm = $name;
if (nm) { it.name = String(nm); }
return __AMI.s({ ok: true, name: String(it.name), typename: String(it.typename), bounds: __AMI.bounds(d, it) });
""")

POLYGON_TMPL = Template(r"""
var d = __AMI.doc();
var st = $st;
var cx = __AMI.X(d, $cx);
var cy = __AMI.Y(d, $cy);
var it = __AMI.withLayer(d, $layer, function () {
  return d.pathItems.polygon(cx, cy, $radius, $sides);
});
__AMI.style(it, st);
var nm = $name;
if (nm) { it.name = String(nm); }
return __AMI.s({ ok: true, name: String(it.name), typename: String(it.typename), bounds: __AMI.bounds(d, it) });
""")

STAR_TMPL = Template(r"""
var d = __AMI.doc();
var st = $st;
var cx = __AMI.X(d, $cx);
var cy = __AMI.Y(d, $cy);
var it = __AMI.withLayer(d, $layer, function () {
  return d.pathItems.star(cx, cy, $outer, $inner, $points);
});
__AMI.style(it, st);
var nm = $name;
if (nm) { it.name = String(nm); }
return __AMI.s({ ok: true, name: String(it.name), typename: String(it.typename), bounds: __AMI.bounds(d, it) });
""")

LINE_TMPL = Template(r"""
var d = __AMI.doc();
var it = __AMI.withLayer(d, $layer, function () { return d.pathItems.add(); });
it.setEntirePath([[__AMI.X(d, $x1), __AMI.Y(d, $y1)], [__AMI.X(d, $x2), __AMI.Y(d, $y2)]]);
it.filled = false;
it.stroked = true;
var sc = __AMI.color($stroke);
if (sc) { it.strokeColor = sc; }
it.strokeWidth = $sw;
var nm = $name;
if (nm) { it.name = String(nm); }
return __AMI.s({ ok: true, name: String(it.name), typename: String(it.typename), bounds: __AMI.bounds(d, it) });
""")

PATH_TMPL = Template(r"""
var d = __AMI.doc();
var res = __AMI.drawPath(d, $data, $layer, $st, $name);
return __AMI.s({ ok: res.count > 0, count: res.count, bounds: res.bounds, warnings: res.warnings });
""")

TEXT_TMPL = Template(r"""
var d = __AMI.doc();
var tf = __AMI.withLayer(d, $layer, function () {
  var t = d.textFrames.add();
  t.contents = $text;
  return t;
});
tf.position = [__AMI.X(d, $x), __AMI.Y(d, $y)];
var ca = tf.textRange.characterAttributes;
ca.size = $size;
var fontName = $font;
var warning = null;
if (fontName) {
  try { ca.textFont = app.textFonts.getByName(String(fontName)); }
  catch (e) { warning = '字体未找到: ' + fontName + '（用 ai_list_fonts 查询可用字体名）'; }
}
var col = __AMI.color($color);
if (col) { ca.fillColor = col; }
var op = $opacity;
if (op !== null) { tf.opacity = op; }
// 校准：让文字外框的左上角正好落在 (x, y)
var bb = __AMI.bounds(d, tf);
if (bb) {
  var pos = tf.position;
  tf.position = [pos[0] + ($x - bb.left), pos[1] + (bb.top - $y)];
  bb = __AMI.bounds(d, tf);
}
var nm = $name;
if (nm) { tf.name = String(nm); }
return __AMI.s({ ok: true, name: String(tf.name), warning: warning, bounds: bb });
""")

SET_STYLE_TMPL = Template(r"""
var d = __AMI.doc();
var targetName = $name;
var it = null;
if (targetName) {
  it = __AMI.itemByName(targetName);
} else {
  var sel = app.selection;
  if (!sel || sel.length === 0) { throw new Error('没有指定 name，且当前没有选中任何对象'); }
  it = sel[0];
}
__AMI.style(it, $st);
var nn = $new_name;
if (nn) { it.name = String(nn); }
return __AMI.s({ ok: true, name: String(it.name), typename: String(it.typename), bounds: __AMI.bounds(d, it) });
""")

TRANSFORM_TMPL = Template(r"""
var d = __AMI.doc();
var it = __AMI.itemByName($name);
var dx = $dx, dy = $dy, sx = $sx, sy = $sy, rot = $rot;
var ops = [];
if (dx !== null || dy !== null) {
  var tdx = (dx === null ? 0 : dx), tdy = (dy === null ? 0 : dy);
  it.translate(tdx, -tdy);
  ops.push('translate');
}
if (sx !== null) { it.resize(sx * 100, (sy === null ? sx : sy) * 100); ops.push('scale'); }
if (rot !== null) { it.rotate(rot); ops.push('rotate'); }
return __AMI.s({ ok: true, name: String(it.name), ops: ops, bounds: __AMI.bounds(d, it) });
""")

DELETE_TMPL = Template(r"""
var d = __AMI.doc();
var removed = 0;
var targetName = $name;
if (targetName) {
  __AMI.itemByName(targetName).remove();
  removed = 1;
} else {
  var sel = app.selection;
  if (!sel || sel.length === 0) { throw new Error('没有指定 name，且当前没有选中任何对象'); }
  var i;
  for (i = sel.length - 1; i >= 0; i--) { sel[i].remove(); removed += 1; }
}
return __AMI.s({ ok: true, removed: removed });
""")

OPEN_TMPL = Template(r"""
var f = new File($path);
if (!f.exists) { throw new Error('文件不存在: ' + $path); }
var d = app.open(f);
return __AMI.s({ ok: true, document: String(d.name), path: String(f.fsName) });
""")

SAVE_TMPL = Template(r"""
var d = __AMI.doc();
var p = $path;
if (p) { d.saveAs(new File(p)); } else { d.save(); }
var full = '';
try { full = String(d.fullName.fsName); } catch (e) { full = ''; }
return __AMI.s({ ok: true, path: full });
""")

CLOSE_DOC_TMPL = Template(r"""
var target = $name;
var d = null, i;
if (target) {
  for (i = 0; i < app.documents.length; i++) {
    if (String(app.documents[i].name) === String(target)) { d = app.documents[i]; break; }
  }
  if (!d) { throw new Error('找不到名为 "' + target + '" 的文档'); }
} else {
  d = __AMI.doc();
}
var nm = String(d.name);
var sv = $save;
d.close(sv ? SaveOptions.SAVECHANGES : SaveOptions.DONOTSAVECHANGES);
return __AMI.s({ ok: true, closed: nm, saved: sv });
""")

EXPORT_TMPL = Template(r"""
var d = __AMI.doc();
var f = new File($path);
var fmt = $fmt;
var warning = null;
if (fmt === 'png') {
  var o = new ExportOptionsPNG24();
  o.antiAliasing = true;
  o.transparency = true;
  o.artBoardClipping = true;
  o.horizontalScale = $scale_pct;
  o.verticalScale = $scale_pct;
  d.exportFile(f, ExportType.PNG24, o);
} else if (fmt === 'jpg') {
  var oj = new ExportOptionsJPEG();
  oj.qualitySetting = $quality;
  oj.artBoardClipping = true;
  oj.horizontalScale = $scale_pct;
  oj.verticalScale = $scale_pct;
  d.exportFile(f, ExportType.JPEG, oj);
} else if (fmt === 'svg') {
  var os = new ExportOptionsSVG();
  try { os.coordinatePrecision = 3; os.fontSubsetting = SVGFontSubsetting.GLYPHSUSED; } catch (e) { }
  d.exportFile(f, ExportType.SVG, os);
  warning = 'Illustrator 的脚本化 SVG 导出会把当前文档关联为该 .svg 文件（文档名随之改变、关闭时不再提示未保存）。要保留可编辑工程，请先 ai_save 存成 .ai。';
} else if (fmt === 'pdf') {
  d.saveAs(f, new PDFSaveOptions());
} else if (fmt === 'ai') {
  d.saveAs(f);
} else {
  throw new Error('不支持的导出格式: ' + fmt);
}
return __AMI.s({ ok: true, path: String(f.fsName), format: fmt, warning: warning });
""")

LIST_ITEMS_TMPL = Template(r"""
var d = __AMI.doc();
var cap = $cap;
var arr = [], i;
for (i = 0; i < d.pageItems.length && arr.length < cap; i++) {
  var it = d.pageItems[i];
  var b = __AMI.bounds(d, it);
  arr.push({
    name: String(it.name),
    typename: String(it.typename),
    bounds: b
  });
}
var layers = [], j;
for (j = 0; j < d.layers.length; j++) { layers.push(String(d.layers[j].name)); }
return __AMI.s({ ok: true, count: d.pageItems.length, items: arr, layers: layers, activeDocument: String(d.name) });
""")

LIST_FONTS_TMPL = Template(r"""
var q = $query;
q = q ? String(q).toLowerCase() : '';
var lim = $limit;
var out = [], i;
for (i = 0; i < app.textFonts.length && out.length < lim; i++) {
  var tf = app.textFonts[i];
  var nm = String(tf.name), fam = String(tf.family);
  if (!q || nm.toLowerCase().indexOf(q) >= 0 || fam.toLowerCase().indexOf(q) >= 0) {
    out.push({ name: nm, family: fam, style: String(tf.style) });
  }
}
return __AMI.s({ ok: true, total: app.textFonts.length, shown: out.length, fonts: out });
""")

RUN_TMPL = Template(r"""
var __r = (function () {
$code
})();
return __AMI.s({ ok: true, result: (typeof __r === 'undefined' ? null : __r) });
""")


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------

def h_status(a: dict) -> list:
    try:
        data, _meta = ai_bridge.run_jsx(STATUS_TMPL.substitute(), auto_launch=False)
    except ai_bridge.AppNotInstalled:
        return [text("Illustrator：未安装（找不到 Illustrator.Application）。")]
    except ai_bridge.AppNotRunning:
        return [text("Illustrator：已安装，当前未运行。提示：ai_launch 可启动它（首次约 1 分钟）。")]
    info = {
        "running": True,
        "version": data.get("version"),
        "documents": data.get("documents"),
        "activeDocument": data.get("activeDocument"),
    }
    return [text("Illustrator：运行中\n" + json.dumps(info, ensure_ascii=False, indent=1))]


def h_launch(a: dict) -> list:
    data, meta = ai_bridge.run_jsx(STATUS_TMPL.substitute(), auto_launch=True)
    lead = "Illustrator 已在运行。" if meta.get("was_running") else "已启动 Illustrator（首次启动要多等一会）。"
    return [text(lead + " 版本：" + str(data.get("version") or "未知") +
                 "；打开的文档数：" + str(data.get("documents"))) ]


def h_new_document(a: dict) -> list:
    unit = str(a.get("unit") or "pt").lower()
    factors = {"pt": 1.0, "px": 1.0, "mm": 72.0 / 25.4, "cm": 72.0 / 2.54, "in": 72.0}
    if unit not in factors:
        raise ToolError("不支持的 unit：%s（可用 pt / px / mm / cm / in）" % unit)
    w = round(num(a, "width", 800) * factors[unit], 2)
    h = round(num(a, "height", 600) * factors[unit], 2)
    mode = str(a.get("color_mode") or "RGB").upper()
    if mode not in ("RGB", "CMYK"):
        raise ToolError("color_mode 只能是 RGB 或 CMYK")
    body = NEW_DOC_TMPL.substitute(w=J(w), h=J(h), color_mode=J(mode))
    return jsx_result(body)


def h_add_rect(a: dict) -> list:
    req(a, "x", "y", "width", "height")
    body = RECT_TMPL.substitute(
        st=J(_style_full(a)),
        layer=J(a.get("layer")),
        r=J(max(0.0, num(a, "corner_radius", 0))),
        y=J(num(a, "y")),
        x=J(num(a, "x")),
        w=J(num(a, "width")),
        h=J(num(a, "height")),
        name=J(a.get("name")),
    )
    return jsx_result(body)


def h_add_ellipse(a: dict) -> list:
    req(a, "cx", "cy", "rx", "ry")
    body = ELLIPSE_TMPL.substitute(
        st=J(_style_full(a)),
        layer=J(a.get("layer")),
        cx=J(num(a, "cx")), cy=J(num(a, "cy")),
        rx=J(num(a, "rx")), ry=J(num(a, "ry")),
        name=J(a.get("name")),
    )
    return jsx_result(body)


def h_add_polygon(a: dict) -> list:
    req(a, "cx", "cy", "radius", "sides")
    sides = int(num(a, "sides"))
    if sides < 3 or sides > 60:
        raise ToolError("sides 需要在 3-60 之间")
    body = POLYGON_TMPL.substitute(
        st=J(_style_full(a)),
        layer=J(a.get("layer")),
        cx=J(num(a, "cx")), cy=J(num(a, "cy")),
        radius=J(num(a, "radius")), sides=J(sides),
        name=J(a.get("name")),
    )
    return jsx_result(body)


def h_add_star(a: dict) -> list:
    req(a, "cx", "cy", "outer_radius", "inner_radius", "points")
    points = int(num(a, "points"))
    if points < 3 or points > 60:
        raise ToolError("points 需要在 3-60 之间")
    body = STAR_TMPL.substitute(
        st=J(_style_full(a)),
        layer=J(a.get("layer")),
        cx=J(num(a, "cx")), cy=J(num(a, "cy")),
        outer=J(num(a, "outer_radius")), inner=J(num(a, "inner_radius")),
        points=J(points),
        name=J(a.get("name")),
    )
    return jsx_result(body)


def h_add_line(a: dict) -> list:
    req(a, "x1", "y1", "x2", "y2")
    body = LINE_TMPL.substitute(
        layer=J(a.get("layer")),
        x1=J(num(a, "x1")), y1=J(num(a, "y1")),
        x2=J(num(a, "x2")), y2=J(num(a, "y2")),
        stroke=J(a.get("stroke", "#000000")),
        sw=J(a.get("stroke_width", 1)),
        name=J(a.get("name")),
    )
    return jsx_result(body)


def h_add_path(a: dict) -> list:
    req(a, "d")
    d = str(a["d"])
    if not d.strip():
        raise ToolError("参数 d 不能是空字符串")
    body = PATH_TMPL.substitute(
        data=J(d),
        layer=J(a.get("layer")),
        st=J(_style_full(a)),
        name=J(a.get("name")),
    )
    return jsx_result(body)


def h_add_text(a: dict) -> list:
    req(a, "text")
    body = TEXT_TMPL.substitute(
        text=J(str(a["text"])),
        x=J(num(a, "x", 72)), y=J(num(a, "y", 72)),
        size=J(num(a, "size", 24)),
        font=J(a.get("font")),
        color=J(a.get("color", "#000000")),
        opacity=J(a.get("opacity")),
        layer=J(a.get("layer")),
        name=J(a.get("name")),
    )
    return jsx_result(body)


def h_set_style(a: dict) -> list:
    body = SET_STYLE_TMPL.substitute(
        name=J(a.get("name")),
        st=J(_style_partial(a)),
        new_name=J(a.get("new_name")),
    )
    return jsx_result(body)


def h_transform(a: dict) -> list:
    req(a, "name")
    if all(a.get(k) is None for k in ("dx", "dy", "scale_x", "scale_y", "rotate")):
        raise ToolError("至少提供 dx / dy / scale_x / scale_y / rotate 之一")
    body = TRANSFORM_TMPL.substitute(
        name=J(a.get("name")),
        dx=J(a.get("dx")), dy=J(a.get("dy")),
        sx=J(a.get("scale_x")), sy=J(a.get("scale_y")),
        rot=J(a.get("rotate")),
    )
    return jsx_result(body)


def h_delete(a: dict) -> list:
    return jsx_result(DELETE_TMPL.substitute(name=J(a.get("name"))))


def h_open(a: dict) -> list:
    req(a, "path")
    return jsx_result(OPEN_TMPL.substitute(path=J(_norm_path(a["path"]))))


def h_save(a: dict) -> list:
    path = a.get("path")
    return jsx_result(SAVE_TMPL.substitute(path=J(_norm_path(path)) if path else J(None)))


def h_close_document(a: dict) -> list:
    return jsx_result(CLOSE_DOC_TMPL.substitute(name=J(a.get("name")), save=J(bool(a.get("save", False)))))


def h_export(a: dict) -> list:
    req(a, "path")
    path = os.path.abspath(os.path.expanduser(str(a["path"])))
    fmt = str(a.get("format") or "png").lower().lstrip(".")
    if fmt not in ("png", "jpg", "svg", "pdf", "ai"):
        raise ToolError("不支持的格式：%s（可用 png / jpg / svg / pdf / ai）" % fmt)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    scale = num(a, "scale", 1.0)
    body = EXPORT_TMPL.substitute(
        path=J(path.replace("\\", "/")),
        fmt=J(fmt),
        scale_pct=J(round(scale * 100, 2)),
        quality=J(int(num(a, "quality", 90))),
    )
    data, _meta = ai_bridge.run_jsx(body)
    content = [text(json.dumps(data, ensure_ascii=False, indent=1))]
    preview = a.get("preview", True)
    if preview and fmt in ("png", "jpg") and os.path.exists(path):
        size = os.path.getsize(path)
        if size <= PREVIEW_MAX_BYTES:
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            content.append({
                "type": "image",
                "data": b64,
                "mimeType": "image/png" if fmt == "png" else "image/jpeg",
            })
        else:
            content[0]["text"] += "\n（图片 %.1fMB 超过预览上限 %.0fMB，未内嵌）" % (
                size / 1048576.0, PREVIEW_MAX_BYTES / 1048576.0)
    return content


def h_list_items(a: dict) -> list:
    cap = int(num(a, "limit", 200))
    cap = max(1, min(cap, 300))
    return jsx_result(LIST_ITEMS_TMPL.substitute(cap=J(cap)))


def h_list_fonts(a: dict) -> list:
    limit = int(num(a, "limit", 60))
    limit = max(1, min(limit, 200))
    return jsx_result(LIST_FONTS_TMPL.substitute(query=J(a.get("query")), limit=J(limit)))


def h_run_script(a: dict) -> list:
    code = a.get("code")
    if not code or not str(code).strip():
        raise ToolError("缺少参数：code（一段 ExtendScript，可以用 return）")
    body = RUN_TMPL.substitute(code=str(code).replace("$", "$$"))
    return jsx_result(body)


def _norm_path(p) -> str:
    return os.path.abspath(os.path.expanduser(str(p))).replace("\\", "/")


# ---------------------------------------------------------------------------
# 工具清单（description 直接给模型看，写得细一点）
# ---------------------------------------------------------------------------

COLOR_FIELD = {
    "anyOf": [{"type": "string"}, {"type": "object"}],
    "description": "颜色：'#RRGGBB' 十六进制 / 'none' 表示无 / {r,g,b}（0-255）/ {c,m,y,k}（0-100）/ {gray}（0-1）",
}


def _style_props() -> dict:
    return {
        "fill": dict(COLOR_FIELD, description="填充色，默认 '#000000'。" + COLOR_FIELD["description"]),
        "stroke": dict(COLOR_FIELD, description="描边色，默认 'none'。" + COLOR_FIELD["description"]),
        "stroke_width": {"type": "number", "description": "描边宽度（pt），默认 1"},
        "opacity": {"type": "number", "description": "不透明度 0-100，默认 100"},
        "layer": {"type": "string", "description": "放进哪个图层；不存在会自动新建，默认当前图层"},
        "name": {"type": "string", "description": "对象名称，之后可用 ai_set_style / ai_transform / ai_delete 定位它"},
    }


TOOL_DEFS = [
    {
        "name": "ai_status",
        "description": "查看 Illustrator 状态：是否安装、是否正在运行、版本、打开的文档。不会启动 Illustrator。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "ai_launch",
        "description": "确保 Illustrator 正在运行（没运行就启动）。首次启动可能要 1 分钟左右，启动后所有画图工具都可用。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "ai_new_document",
        "description": "新建一个 Illustrator 文档（画板）。坐标原点在画板左上角、y 轴向下，单位是点 pt（72pt = 1 英寸）。返回的 document 是文档当前名称（未保存的文档由 Illustrator 命名，如'未标题-1'；保存后按文件名）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "width": {"type": "number", "description": "画板宽度，默认 800"},
                "height": {"type": "number", "description": "画板高度，默认 600"},
                "unit": {"type": "string", "enum": ["pt", "px", "mm", "cm", "in"], "description": "width/height 的单位，默认 pt（px 按 72dpi 与 pt 等价）"},
                "color_mode": {"type": "string", "enum": ["RGB", "CMYK"], "description": "色彩模式，默认 RGB"},
            },
        },
    },
    {
        "name": "ai_list_items",
        "description": "列出活动文档里的对象（名称、类型、外框）和图层，方便后续用名字定位对象。",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "number", "description": "最多返回多少个，默认 200，上限 300"}},
        },
    },
    {
        "name": "ai_list_fonts",
        "description": "查询系统已安装字体（返回 PostScript 字体名，可给 ai_add_text 的 font 参数用；中文可搜 'YaHei' 'SourceHan' 'SimSun' 等）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "匹配关键字（字体名或家族名，不区分大小写），留空列出前若干"},
                "limit": {"type": "number", "description": "最多返回多少个，默认 60，上限 200"},
            },
        },
    },
    {
        "name": "ai_add_rect",
        "description": "画一个矩形。(x, y) 是左上角，y 向下为正。corner_radius 大于 0 时画圆角矩形（半径不超过短边一半）。",
        "inputSchema": {
            "type": "object",
            "properties": dict({
                "x": {"type": "number", "description": "左上角 x"},
                "y": {"type": "number", "description": "左上角 y"},
                "width": {"type": "number", "description": "宽"},
                "height": {"type": "number", "description": "高"},
                "corner_radius": {"type": "number", "description": "圆角半径，默认 0"},
            }, **_style_props()),
            "required": ["x", "y", "width", "height"],
        },
    },
    {
        "name": "ai_add_ellipse",
        "description": "画一个椭圆/圆。(cx, cy) 是圆心，rx / ry 是横竖半径。",
        "inputSchema": {
            "type": "object",
            "properties": dict({
                "cx": {"type": "number", "description": "圆心 x"},
                "cy": {"type": "number", "description": "圆心 y"},
                "rx": {"type": "number", "description": "横半径"},
                "ry": {"type": "number", "description": "竖半径"},
            }, **_style_props()),
            "required": ["cx", "cy", "rx", "ry"],
        },
    },
    {
        "name": "ai_add_polygon",
        "description": "画一个正多边形。(cx, cy) 是中心，radius 是外接半径，sides 是边数（3-60）。",
        "inputSchema": {
            "type": "object",
            "properties": dict({
                "cx": {"type": "number", "description": "中心 x"},
                "cy": {"type": "number", "description": "中心 y"},
                "radius": {"type": "number", "description": "外接半径"},
                "sides": {"type": "number", "description": "边数 3-60"},
            }, **_style_props()),
            "required": ["cx", "cy", "radius", "sides"],
        },
    },
    {
        "name": "ai_add_star",
        "description": "画一个多角星。(cx, cy) 是中心，outer_radius 外半径，inner_radius 内半径，points 角数（3-60）。",
        "inputSchema": {
            "type": "object",
            "properties": dict({
                "cx": {"type": "number", "description": "中心 x"},
                "cy": {"type": "number", "description": "中心 y"},
                "outer_radius": {"type": "number", "description": "外半径"},
                "inner_radius": {"type": "number", "description": "内半径"},
                "points": {"type": "number", "description": "角数 3-60"},
            }, **_style_props()),
            "required": ["cx", "cy", "outer_radius", "inner_radius", "points"],
        },
    },
    {
        "name": "ai_add_line",
        "description": "画一条直线段，从 (x1, y1) 到 (x2, y2)。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "x1": {"type": "number"}, "y1": {"type": "number"},
                "x2": {"type": "number"}, "y2": {"type": "number"},
                "stroke": dict(COLOR_FIELD, description="线色，默认 '#000000'。" + COLOR_FIELD["description"]),
                "stroke_width": {"type": "number", "description": "线宽（pt），默认 1"},
                "layer": {"type": "string", "description": "图层"},
                "name": {"type": "string", "description": "对象名称"},
            },
            "required": ["x1", "y1", "x2", "y2"],
        },
    },
    {
        "name": "ai_add_path",
        "description": "用 SVG path 数据画任意形状（支持 M/L/H/V/C/S/Q/T/Z，大小写相对/绝对都行）。圆弧 A 命令暂不支持（会退化成直线并提示，圆可用 ai_add_ellipse）。坐标系与文档一致：左上角原点、y 向下。",
        "inputSchema": {
            "type": "object",
            "properties": dict({
                "d": {"type": "string", "description": "SVG path 数据，例如 'M 0 0 L 100 0 L 50 80 Z'"},
            }, **_style_props()),
            "required": ["d"],
        },
    },
    {
        "name": "ai_add_text",
        "description": "添加文字。(x, y) 是文字外框的左上角（已自动校准）。可先用 ai_list_fonts 查字体名。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "文字内容"},
                "x": {"type": "number", "description": "x，默认 72"},
                "y": {"type": "number", "description": "y，默认 72"},
                "size": {"type": "number", "description": "字号（pt），默认 24"},
                "font": {"type": "string", "description": "PostScript 字体名（如 'MicrosoftYaHei'），不填用当前默认字体"},
                "color": dict(COLOR_FIELD, description="文字颜色，默认 '#000000'。" + COLOR_FIELD["description"]),
                "opacity": {"type": "number", "description": "不透明度 0-100"},
                "layer": {"type": "string", "description": "图层"},
                "name": {"type": "string", "description": "对象名称"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "ai_set_style",
        "description": "修改对象的样式（填充、描边、线宽、不透明度）。用 name 定位对象；不填 name 则改当前选中的对象。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "对象名称，不填则用当前选中对象"},
                "fill": dict(COLOR_FIELD, description="新的填充色。" + COLOR_FIELD["description"]),
                "stroke": dict(COLOR_FIELD, description="新的描边色。" + COLOR_FIELD["description"]),
                "stroke_width": {"type": "number", "description": "新线宽（pt）"},
                "opacity": {"type": "number", "description": "新不透明度 0-100"},
                "new_name": {"type": "string", "description": "顺便改个名"},
            },
        },
    },
    {
        "name": "ai_transform",
        "description": "移动 / 缩放 / 旋转一个对象（用 name 定位）。dx/dy 是平移量（y 向下为正）；scale_x/scale_y 是缩放倍数（1 = 原大小，以对象中心为基准）；rotate 是角度（度，正数逆时针）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "对象名称"},
                "dx": {"type": "number", "description": "水平平移量"},
                "dy": {"type": "number", "description": "垂直平移量（向下为正）"},
                "scale_x": {"type": "number", "description": "水平缩放倍数"},
                "scale_y": {"type": "number", "description": "垂直缩放倍数，不填跟 scale_x 一样"},
                "rotate": {"type": "number", "description": "旋转角度（度，顺时针为正）"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "ai_delete",
        "description": "删除对象：给了 name 删那一个；不填 name 删当前选中的所有对象。",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "对象名称，不填则删当前选中"}},
        },
    },
    {
        "name": "ai_open",
        "description": "打开一个本地 .ai / .pdf 等文件为 Illustrator 文档。",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "文件绝对路径"}},
            "required": ["path"],
        },
    },
    {
        "name": "ai_save",
        "description": "保存活动文档。给了 path 则另存为（.ai 格式）；不填则保存到当前路径（未保存过会报错）。",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "另存为的绝对路径（建议 .ai）"}},
        },
    },
    {
        "name": "ai_close_document",
        "description": "关闭文档（默认不保存改动）。不填 name 关当前活动文档。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "文档名，不填则关活动文档"},
                "save": {"type": "boolean", "description": "是否保存改动，默认 false"},
            },
        },
    },
    {
        "name": "ai_export",
        "description": "把活动文档导出为文件：png / svg / jpg / pdf / ai。导出 PNG/JPG 时默认把图片内嵌返回，可直接看到画面。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "导出文件的绝对路径"},
                "format": {"type": "string", "enum": ["png", "jpg", "svg", "pdf", "ai"], "description": "格式，默认 png"},
                "scale": {"type": "number", "description": "导出缩放倍数，默认 1"},
                "quality": {"type": "number", "description": "JPG 质量 1-100，默认 90"},
                "preview": {"type": "boolean", "description": "PNG/JPG 是否把图片内嵌返回，默认 true"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "ai_run_script",
        "description": "高级：直接执行一段 ExtendScript（JSX）代码，可以用 return 返回结果。用于内置工具覆盖不到的精细操作。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "ExtendScript 代码，例如 'return app.activeDocument.name;'"},
            },
            "required": ["code"],
        },
    },
]

HANDLERS = {
    "ai_status": h_status,
    "ai_launch": h_launch,
    "ai_new_document": h_new_document,
    "ai_list_items": h_list_items,
    "ai_list_fonts": h_list_fonts,
    "ai_add_rect": h_add_rect,
    "ai_add_ellipse": h_add_ellipse,
    "ai_add_polygon": h_add_polygon,
    "ai_add_star": h_add_star,
    "ai_add_line": h_add_line,
    "ai_add_path": h_add_path,
    "ai_add_text": h_add_text,
    "ai_set_style": h_set_style,
    "ai_transform": h_transform,
    "ai_delete": h_delete,
    "ai_open": h_open,
    "ai_save": h_save,
    "ai_close_document": h_close_document,
    "ai_export": h_export,
    "ai_run_script": h_run_script,
}


def call_tool(name: str, args: dict):
    """执行工具，返回 (content, is_error)。测试脚本也用这个入口。"""
    fn = HANDLERS.get(name)
    if fn is None:
        return [text("未知工具：%s" % name)], True
    try:
        return fn(args or {}), False
    except ToolError as e:
        return [text(str(e))], True
    except ai_bridge.AppNotInstalled:
        return [text("未检测到 Adobe Illustrator（找不到 Illustrator.Application）。请先安装 Illustrator（2020 或更新版本）。")], True
    except ai_bridge.AppNotRunning:
        return [text("Illustrator 当前没有运行。先调用 ai_launch（或直接调画图工具，会自动启动）。")], True
    except ai_bridge.BridgeError as e:
        return [text("Illustrator 调用失败：" + str(e))], True
    except Exception as e:  # noqa: BLE001
        log(traceback.format_exc())
        return [text("内部错误：%s" % e)], True


# ---------------------------------------------------------------------------
# MCP stdio 主循环
# ---------------------------------------------------------------------------

INSTRUCTIONS = (
    "Adobe Illustrator 矢量图工具（adobe-ai-mcp）。建议流程："
    "ai_status 探状态 → ai_new_document 新建画板 → 用 ai_add_rect / ai_add_ellipse / "
    "ai_add_polygon / ai_add_star / ai_add_line / ai_add_path / ai_add_text 画内容 → "
    "ai_export 导出 png 直接看效果 → 不满意用 ai_set_style / ai_transform / ai_delete 修改。"
    "坐标系：原点在画板左上角，y 轴向下，单位 pt。给对象起 name 方便后续定位。"
)


def handle(msg: dict) -> None:
    if not isinstance(msg, dict):
        return
    method = msg.get("method")
    mid = msg.get("id")
    if method is None or str(method).startswith("notifications/"):
        return
    try:
        if method == "initialize":
            params = msg.get("params") or {}
            ver = params.get("protocolVersion") or DEFAULT_PROTOCOL
            if ver not in KNOWN_PROTOCOLS:
                ver = DEFAULT_PROTOCOL
            send({
                "jsonrpc": "2.0", "id": mid,
                "result": {
                    "protocolVersion": ver,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    "instructions": INSTRUCTIONS,
                },
            })
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOL_DEFS}})
        elif method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            content, is_err = call_tool(name, args)
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": content, "isError": is_err}})
        else:
            if mid is not None:
                send({"jsonrpc": "2.0", "id": mid,
                      "error": {"code": -32601, "message": "Method not found: %s" % method}})
    except Exception as e:  # noqa: BLE001
        log(traceback.format_exc())
        if mid is not None:
            send({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32603, "message": "Internal error: %s" % e}})


def main() -> None:
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    log("started, python %s, %d tools" % (sys.version.split()[0], len(TOOL_DEFS)))
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            log("忽略无法解析的行（前 120 字符）：%s" % line[:120])
            continue
        handle(msg)


if __name__ == "__main__":
    main()
