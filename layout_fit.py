"""Fit report text into the template's fixed boxes.

Word text boxes do not grow: text that does not fit is simply hidden. After the report is
rendered, this module estimates how tall each content block will be in Word (using real font
metrics), then for every box that would overflow:

1. grows the box (text box + its rounded background, pushing anything below it inside the same
   group) as far as the free space on that page allows, and
2. if that is still not enough, reduces the font size of that box's content in half-point steps.

All positions/sizes in DrawingML are EMU (914400 per inch, 12700 per point). Word paragraph
units are twips (20 per point) and half-points for font sizes.
"""

from __future__ import annotations

import math
import os
import re
from functools import lru_cache
from pathlib import Path

from docx.oxml.ns import qn

EMU_PER_PT = 12700

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
WPS_NS = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
WPG_NS = "http://schemas.microsoft.com/office/word/2010/wordprocessingGroup"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"

# --- Page geometry shared by every template (A4, footer band starts at ~801pt) -------------
PAGE_CONTENT_BOTTOM_PT = 788  # lowest point a box may reach, leaving a gap above the footer
CHIEF_COMPLAINTS_MAX_GROW_PT = 60  # free gap between Chief Complaints and General Examination

# Medicines block that follows the Course box on the same page (default templates): the
# "Medicines" banner and gaps take this much space between the Course box and the table; the
# table itself (incl. the note row) is measured row by row.
MEDICINES_ABOVE_TABLE_PT = 143
MEDICINES_SAFETY_PT = 14

MIN_FONT_HALF_POINTS = 14  # never shrink content below 7pt
SAFETY_FACTOR = 1.04  # estimates are slightly conservative so Word never clips a line
SAFETY_EXTRA_PT = 3
GROW_SLACK_PT = 2  # grow a little beyond the estimate so rounding never forces a font shrink

DEFAULT_LR_INSET = 91440
DEFAULT_TB_INSET = 45720

FONT_DIRS = [
    Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts",
    Path("/usr/share/fonts"),
    Path("/usr/local/share/fonts"),
    Path(__file__).resolve().parent / "fonts",
]

# Font files to try, in order. The Liberation/Carlito/DejaVu families are metric-compatible
# (or close) substitutes available on Linux servers (Debian: fonts-liberation, fonts-crosextra-carlito).
FONT_FILES = {
    ("times new roman", False): ["times.ttf", "LiberationSerif-Regular.ttf"],
    ("times new roman", True): ["timesbd.ttf", "LiberationSerif-Bold.ttf"],
    ("arial", False): ["arial.ttf", "LiberationSans-Regular.ttf"],
    ("arial", True): ["arialbd.ttf", "LiberationSans-Bold.ttf"],
    ("calibri", False): ["calibri.ttf", "Carlito-Regular.ttf", "LiberationSans-Regular.ttf"],
    ("calibri", True): ["calibrib.ttf", "Carlito-Bold.ttf", "LiberationSans-Bold.ttf"],
    ("tahoma", False): ["tahoma.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf"],
    ("tahoma", True): ["tahomabd.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf"],
}
FALLBACK_FONT = "times new roman"


# ------------------------------------------------------------------------------------------
# Font metrics
# ------------------------------------------------------------------------------------------


@lru_cache(maxsize=None)
def _font_index():
    index = {}
    for directory in FONT_DIRS:
        if not directory.is_dir():
            continue
        try:
            for path in directory.rglob("*"):
                if path.suffix.lower() in {".ttf", ".otf"}:
                    index.setdefault(path.name.lower(), path)
        except OSError:
            continue
    return index


@lru_cache(maxsize=None)
def _load_font(name, bold):
    try:
        from PIL import ImageFont
    except ImportError:  # pragma: no cover - Pillow is a dependency
        return None

    key = (name.lower(), bold)
    candidates = FONT_FILES.get(key) or FONT_FILES.get((FALLBACK_FONT, bold), [])
    index = _font_index()
    for filename in candidates:
        path = index.get(filename.lower())
        if path:
            try:
                return ImageFont.truetype(str(path), size=1000)
            except OSError:
                continue
    return None


def font_available(name, bold=False):
    """True when real metrics (Pillow + a matching font file) are available for this font."""
    return _load_font(name, bold) is not None


def text_width_pt(text, font_name, size_pt, bold=False):
    font = _load_font(font_name, bold)
    if font is None:
        # Rough average advance: capitals are much wider than lower case.
        upper = sum(1 for ch in text if ch.isupper())
        return size_pt * (0.66 * upper + 0.5 * (len(text) - upper))
    return font.getlength(text) / 1000.0 * size_pt


def line_height_pt(font_name, size_pt, bold=False):
    """Natural single line height (ascent + descent) as Word uses for 'auto' spacing."""
    font = _load_font(font_name, bold)
    if font is None:
        return size_pt * 1.15
    ascent, descent = font.getmetrics()
    return (ascent + descent) / 1000.0 * size_pt


def count_lines(text, width_pt, font_name, size_pt, bold=False, first_line_width_pt=None):
    """Greedy word wrap, the way Word breaks lines at spaces."""
    if width_pt <= 0:
        return 1
    total = 0
    for segment in text.split("\n"):
        words = segment.split(" ")
        lines = 1
        current = 0.0
        limit = first_line_width_pt or width_pt
        space = text_width_pt(" ", font_name, size_pt, bold)
        for word in words:
            word_w = text_width_pt(word, font_name, size_pt, bold)
            needed = word_w if current == 0 else current + space + word_w
            if needed <= limit:
                current = needed
                continue
            if current == 0:
                # A single word wider than the line is broken across lines.
                extra = max(1, math.ceil(word_w / limit))
                lines += extra - 1
                current = word_w - (extra - 1) * limit
            else:
                lines += 1
                current = word_w
            limit = width_pt
        total += lines
    return total


# ------------------------------------------------------------------------------------------
# Style resolution (enough of Word's cascade for the content paragraphs we measure)
# ------------------------------------------------------------------------------------------


class StyleResolver:
    def __init__(self, document):
        self.styles = {}
        self.default_rpr = None
        self.default_ppr = None
        self.default_paragraph_style = None
        self.theme_fonts = {"minor": "Calibri", "major": "Calibri Light"}
        try:
            styles_el = document.styles.element
        except Exception:
            styles_el = None
        if styles_el is not None:
            for style in styles_el.iterchildren(qn("w:style")):
                self.styles[style.get(qn("w:styleId"))] = style
                if style.get(qn("w:type")) == "paragraph" and style.get(qn("w:default")) == "1":
                    self.default_paragraph_style = style.get(qn("w:styleId"))
            defaults = styles_el.find(qn("w:docDefaults"))
            if defaults is not None:
                self.default_rpr = defaults.find(f"{qn('w:rPrDefault')}/{qn('w:rPr')}")
                self.default_ppr = defaults.find(f"{qn('w:pPrDefault')}/{qn('w:pPr')}")
        try:
            for part in document.part.package.iter_parts():
                if str(part.partname).startswith("/word/theme/"):
                    xml = part.blob.decode("utf8", "ignore")
                    minor = re.search(r'<a:minorFont>\s*<a:latin typeface="([^"]+)"', xml)
                    major = re.search(r'<a:majorFont>\s*<a:latin typeface="([^"]+)"', xml)
                    if minor:
                        self.theme_fonts["minor"] = minor.group(1)
                    if major:
                        self.theme_fonts["major"] = major.group(1)
                    break
        except Exception:
            pass

    def _style_chain(self, style_id):
        chain = []
        seen = set()
        style_id = style_id or self.default_paragraph_style
        while style_id and style_id not in seen and style_id in self.styles:
            seen.add(style_id)
            style = self.styles[style_id]
            chain.append(style)
            based = style.find(qn("w:basedOn"))
            style_id = based.get(qn("w:val")) if based is not None else None
        return chain

    def _font_from_rfonts(self, rfonts):
        if rfonts is None:
            return None
        for attr in ("w:ascii", "w:hAnsi"):
            value = rfonts.get(qn(attr))
            if value:
                return value
        for attr in ("w:asciiTheme", "w:hAnsiTheme"):
            value = rfonts.get(qn(attr))
            if value:
                return self.theme_fonts["major" if value.startswith("major") else "minor"]
        return None

    def run_props(self, paragraph, run_rpr):
        """(font name, size in pt, bold) for a run in the given paragraph."""
        ppr = paragraph.find(qn("w:pPr"))
        style_id = None
        if ppr is not None and ppr.find(qn("w:pStyle")) is not None:
            style_id = ppr.find(qn("w:pStyle")).get(qn("w:val"))
        layers = [run_rpr] + [style.find(qn("w:rPr")) for style in self._style_chain(style_id)] + [self.default_rpr]

        font = size = bold = None
        for rpr in layers:
            if rpr is None:
                continue
            if font is None:
                font = self._font_from_rfonts(rpr.find(qn("w:rFonts")))
            if size is None and rpr.find(qn("w:sz")) is not None:
                size = int(rpr.find(qn("w:sz")).get(qn("w:val"))) / 2.0
            if bold is None and rpr.find(qn("w:b")) is not None:
                bold = rpr.find(qn("w:b")).get(qn("w:val"), "true") not in ("0", "false")
        return font or self.theme_fonts["minor"], size or 10.0, bool(bold)

    def paragraph_props(self, paragraph):
        ppr = paragraph.find(qn("w:pPr"))
        style_id = None
        if ppr is not None and ppr.find(qn("w:pStyle")) is not None:
            style_id = ppr.find(qn("w:pStyle")).get(qn("w:val"))
        layers = [ppr] + [style.find(qn("w:pPr")) for style in self._style_chain(style_id)] + [self.default_ppr]

        props = {}
        for layer in layers:
            if layer is None:
                continue
            spacing = layer.find(qn("w:spacing"))
            if spacing is not None:
                for key in ("before", "after", "line", "lineRule"):
                    value = spacing.get(qn(f"w:{key}"))
                    if value is not None and key not in props:
                        props[key] = value
            ind = layer.find(qn("w:ind"))
            if ind is not None:
                for key in ("left", "start", "right", "end", "firstLine", "hanging"):
                    value = ind.get(qn(f"w:{key}"))
                    if value is not None and key not in props:
                        props[key] = value
        return props


# ------------------------------------------------------------------------------------------
# Paragraph / text box measurement
# ------------------------------------------------------------------------------------------


def _twips(value):
    try:
        return int(float(value)) / 20.0
    except (TypeError, ValueError):
        return 0.0


def _paragraph_runs(paragraph):
    """Yield (run rPr, text) with tabs/breaks converted, for runs directly in the paragraph."""
    for run in paragraph.iterchildren(qn("w:r")):
        if run.find(f".//{{{WP_NS}}}anchor") is not None or run.find(f".//{{{MC_NS}}}AlternateContent") is not None:
            continue  # floating shapes do not take up lines
        parts = []
        for child in run:
            if child.tag == qn("w:t"):
                parts.append(child.text or "")
            elif child.tag == qn("w:tab"):
                parts.append("    ")
            elif child.tag in (qn("w:br"), qn("w:cr")):
                parts.append("\n")
        yield run.find(qn("w:rPr")), "".join(parts)


def measure_paragraph_pt(paragraph, width_pt, resolver, size_override=None):
    props = resolver.paragraph_props(paragraph)
    runs = list(_paragraph_runs(paragraph))
    text = "".join(text for _, text in runs)

    run_rpr = next((rpr for rpr, t in runs if t.strip()), None)
    if run_rpr is None:
        ppr = paragraph.find(qn("w:pPr"))
        run_rpr = ppr.find(qn("w:rPr")) if ppr is not None else None
    font, size, bold = resolver.run_props(paragraph, run_rpr)
    if size_override is not None:
        size = size_override

    left = _twips(props.get("left", props.get("start", 0)))
    right = _twips(props.get("right", props.get("end", 0)))
    first = _twips(props.get("firstLine", 0)) - _twips(props.get("hanging", 0))
    usable = max(20.0, width_pt - left - right)

    lines = count_lines(text, usable, font, size, bold, first_line_width_pt=max(20.0, usable - first)) if text.strip() else 1

    natural = line_height_pt(font, size, bold)
    rule = props.get("lineRule", "auto")
    line_value = props.get("line")
    if line_value is None:
        line_h = natural
    elif rule == "exact":
        line_h = _twips(line_value)
    elif rule == "atLeast":
        line_h = max(natural, _twips(line_value))
    else:
        line_h = natural * (int(line_value) / 240.0)

    return _twips(props.get("before", 0)) + lines * line_h + _twips(props.get("after", 0))


def _shape_xfrm(shape):
    sppr = shape.find(f"{{{WPS_NS}}}spPr")
    if sppr is None:
        sppr = shape.find(f"{{{A_NS}}}spPr") if shape.find(f"{{{A_NS}}}spPr") is not None else None
    if sppr is None:
        # pictures / nested groups
        xfrm = shape.find(f".//{{{A_NS}}}xfrm")
        return xfrm
    return sppr.find(f"{{{A_NS}}}xfrm")


def _group_of(shape):
    parent = shape.getparent()
    if parent is not None and parent.tag in (f"{{{WPG_NS}}}wgp", f"{{{WPG_NS}}}grpSp"):
        return parent
    return None


def _group_xfrm(group):
    grp_sppr = group.find(f"{{{WPG_NS}}}grpSpPr")
    return grp_sppr.find(f"{{{A_NS}}}xfrm") if grp_sppr is not None else None


def _group_scale(group):
    xfrm = _group_xfrm(group)
    ext = xfrm.find(f"{{{A_NS}}}ext")
    ch_ext = xfrm.find(f"{{{A_NS}}}chExt")
    sx = int(ext.get("cx")) / max(1, int(ch_ext.get("cx")))
    sy = int(ext.get("cy")) / max(1, int(ch_ext.get("cy")))
    return sx, sy


def _anchor_of(element):
    parent = element.getparent()
    while parent is not None and parent.tag != f"{{{WP_NS}}}anchor":
        parent = parent.getparent()
    return parent


def _real_size_emu(shape):
    """Width/height of a shape in real EMU (group scaling applied)."""
    xfrm = _shape_xfrm(shape)
    ext = xfrm.find(f"{{{A_NS}}}ext")
    cx, cy = int(ext.get("cx")), int(ext.get("cy"))
    group = _group_of(shape)
    if group is not None:
        sx, sy = _group_scale(group)
        cx, cy = cx * sx, cy * sy
    return cx, cy


def _insets(shape):
    body_pr = shape.find(f"{{{WPS_NS}}}bodyPr")
    get = (lambda key, default: int(body_pr.get(key, default))) if body_pr is not None else (lambda key, default: default)
    return (
        get("lIns", DEFAULT_LR_INSET),
        get("tIns", DEFAULT_TB_INSET),
        get("rIns", DEFAULT_LR_INSET),
        get("bIns", DEFAULT_TB_INSET),
    )


def _textbox_shape(paragraph):
    """The wps:wsp whose text box holds this paragraph (modern copy only)."""
    parent = paragraph.getparent()
    while parent is not None:
        if parent.tag == f"{{{WPS_NS}}}wsp":
            return parent
        parent = parent.getparent()
    return None


class Box:
    """A content text box plus the paragraphs we rendered into it."""

    def __init__(self, shape, content_groups, resolver):
        self.shape = shape
        self.resolver = resolver
        # All copies (modern + legacy fallback) get the same font size.
        self.all_content = [p for group in content_groups for p in group]
        modern = [group for group in content_groups if group and _textbox_shape(group[0]) is shape]
        self.content = modern[0] if modern else []

    def inner_size_pt(self):
        cx, cy = _real_size_emu(self.shape)
        l_ins, t_ins, r_ins, b_ins = _insets(self.shape)
        return (cx - l_ins - r_ins) / EMU_PER_PT, (cy - t_ins - b_ins) / EMU_PER_PT

    def needed_height_pt(self, content_size_pt=None):
        width, _ = self.inner_size_pt()
        txbx = self.shape.find(f".//{{{W_NS}}}txbxContent")
        total = 0.0
        for paragraph in txbx.iterchildren(qn("w:p")):
            override = content_size_pt if paragraph in self.content else None
            total += measure_paragraph_pt(paragraph, width, self.resolver, size_override=override)
        return total * SAFETY_FACTOR + SAFETY_EXTRA_PT

    def overflow_pt(self, content_size_pt=None):
        _, height = self.inner_size_pt()
        return self.needed_height_pt(content_size_pt) - height

    def content_size_pt(self):
        for paragraph in self.content:
            for rpr, text in _paragraph_runs(paragraph):
                if text.strip():
                    return self.resolver.run_props(paragraph, rpr)[1]
        return None

    def set_content_size(self, size_pt):
        half_points = str(int(round(size_pt * 2)))
        for paragraph in self.all_content:
            for rpr_parent in list(paragraph.iterchildren(qn("w:r"))) + [paragraph.find(qn("w:pPr"))]:
                if rpr_parent is None:
                    continue
                rpr = rpr_parent.find(qn("w:rPr"))
                if rpr is None:
                    rpr = rpr_parent.makeelement(qn("w:rPr"), {})
                    if rpr_parent.tag == qn("w:r"):
                        rpr_parent.insert(0, rpr)
                    else:
                        rpr_parent.append(rpr)
                for tag in ("w:sz", "w:szCs"):
                    el = rpr.find(qn(tag))
                    if el is None:
                        el = rpr.makeelement(qn(tag), {})
                        _insert_rpr_child(rpr, el)
                    el.set(qn("w:val"), half_points)


# Schema order of the rPr children we may need to insert in front of.
_RPR_ORDER = [
    "rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps", "smallCaps", "strike", "dstrike", "outline",
    "shadow", "emboss", "imprint", "noProof", "snapToGrid", "vanish", "webHidden", "color", "spacing",
    "w", "kern", "position", "sz", "szCs", "highlight", "u", "effect", "bdr", "shd", "fitText",
    "vertAlign", "rtl", "cs", "em", "lang", "eastAsianLayout", "specVanish", "oMath",
]


def _insert_rpr_child(rpr, element):
    name = element.tag.split("}")[1]
    order = _RPR_ORDER.index(name) if name in _RPR_ORDER else len(_RPR_ORDER)
    for index, child in enumerate(rpr):
        child_name = child.tag.split("}")[1]
        if child_name in _RPR_ORDER and _RPR_ORDER.index(child_name) > order:
            rpr.insert(index, element)
            return
    rpr.append(element)


# ------------------------------------------------------------------------------------------
# Growing shapes
# ------------------------------------------------------------------------------------------


def _stretch_custom_geometry(shape, old_cy, delta_cy):
    """Extend a custom outline downward while keeping its rounded corners undistorted."""
    if old_cy <= 0:
        return
    for path in shape.iter(f"{{{A_NS}}}path"):
        height = path.get("h")
        if not height:
            continue
        height = int(height)
        if height <= 0:
            continue
        delta_path = int(round(delta_cy * height / old_cy))
        for point in path.iter(f"{{{A_NS}}}pt"):
            y = int(point.get("y"))
            if y > height / 2:
                point.set("y", str(y + delta_path))
        path.set("h", str(height + delta_path))


def _grow_shape_height(shape, delta_cy_local):
    """Increase a shape's height (in its own coordinate space) keeping its top in place."""
    xfrm = _shape_xfrm(shape)
    ext = xfrm.find(f"{{{A_NS}}}ext")
    old = int(ext.get("cy"))
    ext.set("cy", str(old + int(round(delta_cy_local))))
    _stretch_custom_geometry(shape, old, delta_cy_local)


def _shift_shape(shape, delta_y_local):
    xfrm = _shape_xfrm(shape)
    off = xfrm.find(f"{{{A_NS}}}off")
    off.set("y", str(int(off.get("y")) + int(round(delta_y_local))))


def _grow_group_extent(group, delta_emu):
    """Make the group (and its anchor) taller by delta real EMU without rescaling children."""
    _, sy = _group_scale(group)
    xfrm = _group_xfrm(group)
    ext = xfrm.find(f"{{{A_NS}}}ext")
    ch_ext = xfrm.find(f"{{{A_NS}}}chExt")
    ext.set("cy", str(int(ext.get("cy")) + int(delta_emu)))
    ch_ext.set("cy", str(int(ch_ext.get("cy")) + int(round(delta_emu / sy))))
    if group.tag == f"{{{WPG_NS}}}wgp":
        anchor = _anchor_of(group)
        if anchor is not None:
            extent = anchor.find(f"{{{WP_NS}}}extent")
            extent.set("cy", str(int(extent.get("cy")) + int(delta_emu)))


def _shape_y_range(shape):
    xfrm = _shape_xfrm(shape)
    off = xfrm.find(f"{{{A_NS}}}off")
    ext = xfrm.find(f"{{{A_NS}}}ext")
    y = int(off.get("y"))
    return y, y + int(ext.get("cy"))


def grow_box(box, delta_pt, extra_backgrounds=()):
    """Make a content box taller by delta_pt.

    Inside a group, shapes that span the box (its backgrounds) grow with it and shapes below it
    move down. A stand-alone text box grows together with the given background shapes.
    """
    if delta_pt <= 0:
        return
    delta_emu = delta_pt * EMU_PER_PT
    shape = box.shape
    group = _group_of(shape)

    if group is not None:
        _, sy = _group_scale(group)
        delta_local = delta_emu / sy
        top, bottom = _shape_y_range(shape)
        tolerance = 10 * EMU_PER_PT / sy
        siblings = [child for child in group if _shape_xfrm(child) is not None and child.tag != f"{{{WPG_NS}}}grpSpPr"]
        for child in siblings:
            c_top, c_bottom = _shape_y_range(child)
            if child is shape:
                _grow_shape_height(child, delta_local)
            elif c_top >= bottom - tolerance:
                _shift_shape(child, delta_local)
            elif c_bottom >= bottom - tolerance and c_top <= top + tolerance * 2:
                _grow_shape_height(child, delta_local)
        _grow_group_extent(group, delta_emu)
    else:
        _grow_shape_height(shape, delta_emu)
        anchor = _anchor_of(shape)
        if anchor is not None:
            extent = anchor.find(f"{{{WP_NS}}}extent")
            extent.set("cy", str(int(extent.get("cy")) + int(delta_emu)))

    for background in extra_backgrounds:
        bg_group = _group_of(background)
        if bg_group is not None:
            _, sy = _group_scale(bg_group)
            _grow_shape_height(background, delta_emu / sy)
            # Extend the group if the background now reaches past it.
            xfrm = _group_xfrm(bg_group)
            ch_off = int(xfrm.find(f"{{{A_NS}}}chOff").get("y"))
            ch_ext = int(xfrm.find(f"{{{A_NS}}}chExt").get("cy"))
            overhang = _shape_y_range(background)[1] - (ch_off + ch_ext)
            if overhang > 0:
                _grow_group_extent(bg_group, overhang * sy)
        else:
            _grow_shape_height(background, delta_emu)
            anchor = _anchor_of(background)
            if anchor is not None:
                extent = anchor.find(f"{{{WP_NS}}}extent")
                extent.set("cy", str(int(extent.get("cy")) + int(delta_emu)))


# ------------------------------------------------------------------------------------------
# Locating boxes and their page positions
# ------------------------------------------------------------------------------------------


def _anchor_page_top_emu(anchor):
    pos_v = anchor.find(f"{{{WP_NS}}}positionV")
    if pos_v is None or pos_v.get("relativeFrom") != "page":
        return None
    offset = pos_v.find(f"{{{WP_NS}}}posOffset")
    return int(offset.text) if offset is not None and offset.text else None


def _page_bottom_pt(shape):
    """Page-relative bottom (pt) of a shape, or None if its anchor is not page-positioned."""
    anchor = _anchor_of(shape)
    if anchor is None:
        return None
    top = _anchor_page_top_emu(anchor)
    if top is None:
        return None
    group = _group_of(shape)
    if group is None:
        return (top + _real_size_emu(shape)[1]) / EMU_PER_PT
    _, sy = _group_scale(group)
    ch_off = int(_group_xfrm(group).find(f"{{{A_NS}}}chOff").get("y"))
    _, bottom = _shape_y_range(shape)
    return (top + (bottom - ch_off) * sy) / EMU_PER_PT


def _visible_group_bottom_pt(shape):
    """Lowest page point of the non-text shapes in the content box's group."""
    group = _group_of(shape)
    if group is None:
        return _page_bottom_pt(shape)
    bottoms = []
    for child in group:
        if child.tag != f"{{{WPS_NS}}}wsp" or child.find(f"{{{WPS_NS}}}txbx") is not None:
            continue
        bottom = _page_bottom_pt(child)
        if bottom is not None:
            bottoms.append(bottom)
    return max(bottoms) if bottoms else _page_bottom_pt(shape)


def _body_index(document, element):
    body = document.element.body
    node = element
    while node is not None and node.getparent() is not body:
        node = node.getparent()
    return list(body).index(node) if node is not None else None


def find_backgrounds(document, box, max_distance=4):
    """Rounded background shapes drawn around a stand-alone text box.

    They are separate shapes (sometimes inside a decorative group) close to the text box in the
    document, slightly wider than it and of similar height.
    """
    width_emu, height_emu = _real_size_emu(box.shape)
    box_index = _body_index(document, box.shape)
    found = []
    for shape in document.element.body.iter(f"{{{WPS_NS}}}wsp"):
        if shape is box.shape or _shape_xfrm(shape) is None:
            continue
        if shape.find(f".//{{{A_NS}}}custGeom") is None and shape.find(f".//{{{A_NS}}}prstGeom") is None:
            continue
        index = _body_index(document, shape)
        if index is None or box_index is None or abs(index - box_index) > max_distance:
            continue
        cx, cy = _real_size_emu(shape)
        if -10 * EMU_PER_PT <= cx - width_emu <= 60 * EMU_PER_PT and 0.9 * height_emu <= cy <= 1.6 * height_emu:
            found.append(shape)
    return found


def table_height_pt(table, resolver):
    """Estimated rendered height of a table: each row is as tall as its tallest wrapped cell."""
    tbl_pr = table.find(qn("w:tblPr"))
    spacing_pt = 0.0
    margin_lr = 0.0
    if tbl_pr is not None:
        cell_spacing = tbl_pr.find(qn("w:tblCellSpacing"))
        if cell_spacing is not None:
            spacing_pt = _twips(cell_spacing.get(qn("w:w")))
        cell_mar = tbl_pr.find(qn("w:tblCellMar"))
        if cell_mar is not None:
            for side in ("left", "right", "start", "end"):
                el = cell_mar.find(qn(f"w:{side}"))
                if el is not None:
                    margin_lr += _twips(el.get(qn("w:w")))

    grid = [_twips(col.get(qn("w:w"))) for col in table.iter(qn("w:gridCol"))]
    total = spacing_pt
    for row in table.iterchildren(qn("w:tr")):
        min_height = 0.0
        tr_pr = row.find(qn("w:trPr"))
        if tr_pr is not None and tr_pr.find(qn("w:trHeight")) is not None:
            min_height = _twips(tr_pr.find(qn("w:trHeight")).get(qn("w:val")))
        tallest = 0.0
        column = 0
        for cell in row.iterchildren(qn("w:tc")):
            tc_pr = cell.find(qn("w:tcPr"))
            span = 1
            width = None
            if tc_pr is not None:
                if tc_pr.find(qn("w:gridSpan")) is not None:
                    span = int(tc_pr.find(qn("w:gridSpan")).get(qn("w:val")))
                if tc_pr.find(qn("w:tcW")) is not None and tc_pr.find(qn("w:tcW")).get(qn("w:type")) == "dxa":
                    width = _twips(tc_pr.find(qn("w:tcW")).get(qn("w:w")))
            if width is None:
                width = sum(grid[column:column + span]) if grid else 100.0
            column += span
            usable = max(20.0, width - margin_lr - spacing_pt)
            height = sum(measure_paragraph_pt(p, usable, resolver) for p in cell.iterchildren(qn("w:p")))
            tallest = max(tallest, height)
        total += max(min_height, tallest) + spacing_pt
    return total


def _box_for(groups, resolver):
    for group in groups or []:
        if not group:
            continue
        shape = _textbox_shape(group[0])
        if shape is not None and shape.find(f"{{{WPS_NS}}}txbx") is not None:
            return Box(shape, groups, resolver)
    return None


def _shrink_to_fit(box, available_extra_pt=0.0):
    """Reduce the content font in half-point steps until the box (incl. growth) fits."""
    size = box.content_size_pt()
    if size is None:
        return
    current = size
    while box.overflow_pt(current) > available_extra_pt and current * 2 > MIN_FONT_HALF_POINTS:
        current -= 0.5
    if current != size:
        box.set_content_size(current)


def _fit(box, room_pt, backgrounds=()):
    """Grow up to room_pt, then shrink the font if it still does not fit. Returns growth used."""
    overflow = box.overflow_pt()
    if overflow <= 0:
        return 0.0
    grow = max(0.0, min(overflow + GROW_SLACK_PT, room_pt))
    if grow > 0:
        grow_box(box, grow, backgrounds)
    if overflow > grow:
        _shrink_to_fit(box)
    return grow


# ------------------------------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------------------------------


def fit_report_boxes(document, rendered, medication_table=None):
    """Fit Diagnosis, Risk factors, Chief complaints and Course into their boxes.

    medication_table is the (final) medicines table, measured to know how much of the Course
    page it needs.
    """
    resolver = StyleResolver(document)

    # Page 1: Diagnosis and Risk Factors sit side by side; grow them together so their bottoms
    # stay level (Treatment Plan inside the Risk group moves down with it).
    diagnosis = _box_for(rendered.get("diagnosis"), resolver)
    risk = _box_for(rendered.get("riskfactors"), resolver)
    pair = [box for box in (diagnosis, risk) if box is not None]
    if pair:
        bottoms = [_visible_group_bottom_pt(box.shape) for box in pair]
        bottoms = [b for b in bottoms if b is not None]
        room = max(0.0, PAGE_CONTENT_BOTTOM_PT - max(bottoms)) if bottoms else 0.0
        needed = max([0.0] + [box.overflow_pt() for box in pair])
        grow = min(needed + GROW_SLACK_PT, room) if needed > 0 else 0.0
        if grow > 0:
            for box in pair:
                grow_box(box, grow)
        for box in pair:
            if box.overflow_pt() > 0:
                _shrink_to_fit(box)

    # Page 2: Chief complaints (stand-alone text box over separate background shapes).
    chief = _box_for(rendered.get("chief_complaints"), resolver)
    if chief is not None:
        _fit(chief, CHIEF_COMPLAINTS_MAX_GROW_PT, find_backgrounds(document, chief))

    # Course: its background is page-positioned; the medicines block may follow on the same page.
    course = _box_for(rendered.get("course"), resolver)
    if course is not None:
        backgrounds = find_backgrounds(document, course)
        bottoms = [b for b in (_page_bottom_pt(bg) for bg in backgrounds) if b is not None]
        course_bottom = max(bottoms) if bottoms else None
        if course_bottom is None:
            room = 0.0
        else:
            table_pt = table_height_pt(medication_table, resolver) if medication_table is not None else 0.0
            medicines = MEDICINES_ABOVE_TABLE_PT + table_pt + MEDICINES_SAFETY_PT
            if course_bottom + medicines <= PAGE_CONTENT_BOTTOM_PT:
                room = PAGE_CONTENT_BOTTOM_PT - course_bottom - medicines
            else:
                room = PAGE_CONTENT_BOTTOM_PT - course_bottom  # medicines already start a new page
        _fit(course, max(0.0, room), backgrounds)
