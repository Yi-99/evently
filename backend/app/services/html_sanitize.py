"""Sanitiser for model-generated slide HTML (defence in depth; slides also render in sandboxed iframes).

The input is tokenised with the standard-library `html.parser` and re-serialised from scratch,
so the output grammar is tightly controlled: validated tag names, filtered and double-quoted
attributes, entity-escaped text, and `<style>` content with every `<` CSS-escaped. Nothing from
the input is copied through verbatim, which is what makes parser-differential (mXSS) tricks
moot: whatever a browser makes of the *input*, it can only find in the *output* the elements and
attributes this module chose to emit.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

# Elements removed together with their contents. Everything here either executes, loads
# or embeds foreign content, or is a raw-text element whose contents browsers tokenise
# differently from `html.parser`.
_DROP_ELEMENTS = frozenset(
    {
        "script", "iframe", "frame", "frameset", "object", "embed", "applet", "portal", "fencedframe",
        "link", "meta", "base", "form", "template",
        "noscript", "noembed", "noframes", "xmp", "plaintext",
    }
)  # fmt: skip
_VOID_ELEMENTS = frozenset(
    {"area", "base", "basefont", "bgsound", "br", "col", "embed", "frame", "hr", "img", "input",
     "keygen", "link", "meta", "param", "source", "track", "wbr"}
)  # fmt: skip
_FOREIGN_ROOTS = frozenset({"svg", "math"})
# SMIL elements that can animate an attribute; dropped when they target a URL or handler attribute.
_SMIL_ELEMENTS = frozenset({"animate", "set", "animatetransform", "animatemotion", "animatecolor"})

_DROP_ATTRS = frozenset({"srcdoc", "srcset", "imagesrcset", "ping"})
_LINK_ATTRS = frozenset({"href", "xlink:href", "action", "formaction", "cite"})
_RESOURCE_ATTRS = frozenset({"src", "poster", "background", "lowsrc", "dynsrc"})
_URL_ATTRS = _LINK_ATTRS | _RESOURCE_ATTRS
_LINK_SCHEMES = frozenset({"https", "http", "mailto", "tel"})
# `<a>`/`<area>` carry links; `<image>`/`<feImage>` load images; every other href is a same-document ref.
_HREF_LINK_TAGS = frozenset({"a", "area"})
_HREF_IMAGE_TAGS = frozenset({"image", "feimage"})

_TAG_NAME = re.compile(r"[a-z][a-z0-9:-]*")
_ATTR_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_.:-]*")
_URL_NOISE = re.compile(r"[\x00-\x20\x7f]")  # browsers strip leading/trailing C0+space and inner tab/CR/LF
_URL_SCHEME = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*):")
_SCRIPT_SCHEME = re.compile(r"(?:javascript|vbscript):", re.IGNORECASE)
_DATA_IMAGE = re.compile(r"data:image/(?:png|jpe?g|gif|webp|avif|bmp|x-icon|svg\+xml)[;,]", re.IGNORECASE)

# CSS: letters may be written as escapes (`\75rl(`), so they are canonicalised before matching.
_CSS_ESCAPE = re.compile(r"\\(?:([0-9a-fA-F]{1,6})(?:\r\n|[ \t\n\r\f])?|(.))", re.DOTALL)
_CSS_COMMENT = re.compile(r"/\*.*?(?:\*/|\Z)", re.DOTALL)
_CSS_IMPORT = re.compile(r"@import\b[^;]*(?:;|\Z)", re.IGNORECASE)
_CSS_EXPRESSION = re.compile(r"\bexpression\s*\(", re.IGNORECASE)
_CSS_LEGACY_PROPERTY = re.compile(r"(?:-moz-binding|behavior)\s*:", re.IGNORECASE)
_CSS_URL = re.compile(r"""\burl\(\s*(?:"([^"]*)"|'([^']*)'|([^)\s]*))\s*\)?""", re.IGNORECASE)
_CSS_URL_FN = re.compile(r"\burl\(", re.IGNORECASE)

# HTML parsers lowercase every name; these tables (from the HTML spec's "adjust SVG attributes"
# and "adjust SVG tag names") restore the case-sensitive SVG spellings.
_SVG_ATTRIBUTES = {
    name.lower(): name
    for name in (
        "attributeName attributeType baseFrequency baseProfile calcMode clipPathUnits diffuseConstant "
        "edgeMode filterUnits glyphRef gradientTransform gradientUnits kernelMatrix kernelUnitLength "
        "keyPoints keySplines keyTimes lengthAdjust limitingConeAngle markerHeight markerUnits markerWidth "
        "maskContentUnits maskUnits numOctaves pathLength patternContentUnits patternTransform patternUnits "
        "pointsAtX pointsAtY pointsAtZ preserveAlpha preserveAspectRatio primitiveUnits refX refY "
        "repeatCount repeatDur requiredExtensions requiredFeatures specularConstant specularExponent "
        "spreadMethod startOffset stdDeviation stitchTiles surfaceScale systemLanguage tableValues "
        "targetX targetY textLength viewBox viewTarget xChannelSelector yChannelSelector zoomAndPan"
    ).split()
}
_SVG_ELEMENTS = {
    name.lower(): name
    for name in (
        "altGlyph altGlyphDef altGlyphItem animateColor animateMotion animateTransform clipPath feBlend "
        "feColorMatrix feComponentTransfer feComposite feConvolveMatrix feDiffuseLighting feDisplacementMap "
        "feDistantLight feDropShadow feFlood feFuncA feFuncB feFuncG feFuncR feGaussianBlur feImage feMerge "
        "feMergeNode feMorphology feOffset fePointLight feSpecularLighting feSpotLight feTile feTurbulence "
        "foreignObject glyphRef linearGradient radialGradient textPath"
    ).split()
}


def sanitize_slide_html(html_text: str) -> str:
    """Return `html_text` with active content removed.

    Removes <script>, <iframe>, <object>, <embed>, <link>, <meta>, <base>, <form>, <template>
    and the raw-text/legacy elements (<noscript>, <xmp>, ...) together with their contents;
    comments, doctypes and processing instructions; every `on*` event-handler attribute,
    `srcdoc`, `srcset` and `ping`; URL attributes that are not https (links also http/mailto/tel,
    images also `data:image/*`, SVG references only `#fragment`); SMIL animations that target an
    href. In CSS (<style> and style="") it drops `@import`, `expression()`, `behavior`/`-moz-binding`
    and every `url(...)` that is not https, data or a `#fragment`, after undoing escape
    obfuscation. Keeps <style>, inline SVG (case-sensitive names restored) and https/data images.
    Idempotent. Never raises on malformed markup: if the parser fails, the input is returned as
    escaped text.
    """
    if not html_text:
        return ""
    source = html_text.replace("\x00", "")
    parser = _Sanitizer()
    try:
        parser.feed(source)
        parser.close()
    except Exception:  # noqa: BLE001 - fail closed: text can never execute
        return html.escape(source, quote=False)
    return "".join(parser.out)


# --- URLs ---------------------------------------------------------------------------------


def _url_allowed(tag: str, attr: str, value: str) -> bool:
    url = _URL_NOISE.sub("", value)
    if url.startswith("#"):
        return attr in ("href", "xlink:href")
    match = _URL_SCHEME.match(url)
    scheme = match.group(1).lower() if match else ""
    if attr in ("href", "xlink:href"):
        if tag in _HREF_LINK_TAGS:
            return scheme in _LINK_SCHEMES
        if tag not in _HREF_IMAGE_TAGS:
            return False
    elif attr in _LINK_ATTRS:
        return scheme in _LINK_SCHEMES
    return scheme == "https" or bool(_DATA_IMAGE.match(url))


# --- CSS ----------------------------------------------------------------------------------


def _decode_letter_escape(match: re.Match[str]) -> str:
    hex_digits, literal = match.groups()
    code_point = int(hex_digits, 16) if hex_digits else ord(literal)
    if code_point < 0x80 and chr(code_point).isalpha():
        return chr(code_point)
    return match.group(0)


def _check_css_url(match: re.Match[str]) -> str:
    url = _URL_NOISE.sub("", next(group for group in match.groups() if group is not None)).lower()
    if url.startswith(("https:", "data:", "#")):
        return match.group(0)
    return "none"


def _clean_css(css: str) -> str:
    """Neutralise active CSS. Removed constructs become whitespace so tokens can never join up."""
    css = _CSS_ESCAPE.sub(_decode_letter_escape, css)
    css = _CSS_COMMENT.sub(" ", css)
    css = _CSS_IMPORT.sub(" ", css)
    css = _CSS_EXPRESSION.sub(" (", css)
    css = _CSS_LEGACY_PROPERTY.sub("-x-removed:", css)
    return _CSS_URL.sub(_check_css_url, css)


def _style_element_content(css: str) -> str:
    # `<style>` is raw text in HTML but ordinary markup inside <svg>; CSS-escaping `<` keeps the
    # content inert in both readings. CDATA markers (common in exported SVG) are just dropped.
    css = css.replace("<![CDATA[", "").replace("]]>", "")
    return _clean_css(css).replace("<", "\\3C ")


# --- parser -------------------------------------------------------------------------------


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._drop_tag: str | None = None  # element being dropped with its contents
        self._drop_depth = 0
        self._style: list[str] | None = None  # buffered content of an open <style>
        self._foreign = 0  # nesting depth of <svg>/<math>

    # -- helpers --

    def _dropped(self, tag: str, attrs: list[tuple[str, str | None]]) -> bool:
        if tag in _DROP_ELEMENTS:
            return True
        if tag in _SMIL_ELEMENTS:
            target = next((v or "" for k, v in attrs if k == "attributename"), "").strip().lower()
            return target in ("href", "xlink:href") or target.startswith("on")
        return False

    def _start_drop(self, tag: str) -> None:
        self._drop_tag = tag
        self._drop_depth = 1

    def _emit_start(self, tag: str, attrs: list[tuple[str, str | None]], *, self_closing: bool) -> None:
        parts = [f"<{_SVG_ELEMENTS.get(tag, tag)}"]
        for name, value in _clean_attrs(tag, attrs):
            parts.append(f" {name}" if value is None else f' {name}="{html.escape(value, quote=True)}"')
        parts.append(" />" if self_closing else ">")
        self.out.append("".join(parts))

    def _flush_style(self) -> None:
        if self._style is not None:
            self.out.append(_style_element_content("".join(self._style)))
            self.out.append("</style>")
            self._style = None

    # -- HTMLParser callbacks --

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._drop_tag is not None:
            if tag == self._drop_tag and tag not in _VOID_ELEMENTS:
                self._drop_depth += 1
            return
        if not _TAG_NAME.fullmatch(tag):
            return
        if self._dropped(tag, attrs):
            if tag not in _VOID_ELEMENTS:
                self._start_drop(tag)
            return
        self._emit_start(tag, attrs, self_closing=False)
        if tag == "style":
            self._style = []  # the parser is now in raw-text mode; content arrives via handle_data
        elif tag in _FOREIGN_ROOTS:
            self._foreign += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._drop_tag is not None or not _TAG_NAME.fullmatch(tag):
            return
        in_foreign = self._foreign > 0 or tag in _FOREIGN_ROOTS
        if tag == "style":
            # `<style/>` is empty in SVG and an *open* raw-text element in HTML, where the parser
            # would hand us its contents as markup: emit nothing in either case.
            return
        if self._dropped(tag, attrs):
            if tag not in _VOID_ELEMENTS and not in_foreign:
                self._start_drop(tag)  # HTML ignores the slash: `<script/>` opens a script
            return
        if in_foreign:
            self._emit_start(tag, attrs, self_closing=True)
        else:
            self._emit_start(tag, attrs, self_closing=False)  # `<div/>` is just `<div>` in HTML

    def handle_endtag(self, tag: str) -> None:
        if self._drop_tag is not None:
            if tag == self._drop_tag:
                self._drop_depth -= 1
                if self._drop_depth == 0:
                    self._drop_tag = None
            return
        if tag == "style":
            self._flush_style()  # a stray </style> with no open <style> is ignored
            return
        if tag in _VOID_ELEMENTS or tag in _DROP_ELEMENTS or not _TAG_NAME.fullmatch(tag):
            return  # stray end tags of removed elements (e.g. a `</script>` after a bad start tag) go too
        if tag in _FOREIGN_ROOTS and self._foreign:
            self._foreign -= 1
        self.out.append(f"</{_SVG_ELEMENTS.get(tag, tag)}>")

    def handle_data(self, data: str) -> None:
        if self._drop_tag is not None:
            return
        if self._style is not None:
            self._style.append(data)
        else:
            self.out.append(html.escape(data, quote=False))

    def unknown_decl(self, data: str) -> None:
        # `<![CDATA[...]]>`: text inside SVG/MathML, a bogus comment (dropped) in HTML.
        if self._drop_tag is None and self._foreign and data.startswith("CDATA["):
            self.handle_data(data[6:])

    def handle_comment(self, data: str) -> None:
        pass

    def handle_decl(self, decl: str) -> None:
        pass

    def handle_pi(self, data: str) -> None:
        pass

    def close(self) -> None:
        super().close()
        self._flush_style()


def _clean_attrs(tag: str, attrs: list[tuple[str, str | None]]) -> list[tuple[str, str | None]]:
    """Filter and canonicalise attributes; the first occurrence of a name wins, as in browsers."""
    cleaned: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for name, value in attrs:
        if name in seen or not _ATTR_NAME.fullmatch(name) or name.startswith("on") or name in _DROP_ATTRS:
            continue
        seen.add(name)
        if value is not None:
            if name in _URL_ATTRS:
                if not _url_allowed(tag, name, value):
                    continue
            elif _SCRIPT_SCHEME.match(_URL_NOISE.sub("", value)):
                continue  # unknown attribute carrying a script URL (e.g. SMIL `to=`)
            if name == "style" or _CSS_URL_FN.search(_CSS_ESCAPE.sub(_decode_letter_escape, value)):
                value = _clean_css(value)  # style="" and SVG presentation attributes like fill="url(#g)"
        cleaned.append((_SVG_ATTRIBUTES.get(name, name), value))
    return cleaned
