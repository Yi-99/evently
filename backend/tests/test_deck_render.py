"""Deck renderer: document structure, escaping of untrusted input, CSP/nonce wiring."""

import html
import re
from html.parser import HTMLParser

from app.schemas.presentation import DeckTheme
from app.services import deck_render
from app.services.deck_render import (
    STAGE_HEIGHT,
    STAGE_WIDTH,
    deck_csp,
    render_deck_document,
    render_slide_document,
    slide_csp,
    theme_css,
)

HOSTILE_TITLE = '</title><script>alert("t")</script><img src=x onerror=alert(1)> "quoted" & \'single\''
HOSTILE_SLIDE = (
    '<h1 title="a&quot;b">Hi "there" & \'you\'</h1><style>p{color:red}</style>'
    "</iframe></script><script>alert(1)</script><p onclick=x>tail</p>"
)
SLIDES = [
    {"id": "sld_1", "title": "One", "html": "<p>one</p>"},
    {"id": "sld_2", "title": HOSTILE_TITLE, "html": HOSTILE_SLIDE},
    {"id": "sld_3", "title": "Three", "html": '<svg viewBox="0 0 1 1"><circle r="1"/></svg>'},
]


class Outline(HTMLParser):
    """Top-level elements of a document, with their attributes and (for script/style) contents."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.scripts: list[str] = []
        self.title = ""
        self._in: str | None = None

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag in ("script", "title"):
            self._in = tag
            if tag == "script":
                self.scripts.append("")

    def handle_data(self, data):
        if self._in == "script":
            self.scripts[-1] += data
        elif self._in == "title":
            self.title += data

    def handle_endtag(self, tag):
        if tag == self._in:
            self._in = None

    def attrs(self, name: str) -> list[dict[str, str | None]]:
        return [a for t, a in self.tags if t == name]


def outline(document: str) -> Outline:
    parsed = Outline()
    parsed.feed(document)
    parsed.close()
    return parsed


# --- theme -----------------------------------------------------------------------------------


def test_theme_css_maps_every_token_and_defaults_missing_ones():
    css = theme_css({"background": "#123456", "heading_font": "'Inter', sans-serif"})
    assert css.startswith(":root{") and css.endswith("}")
    assert "--bg:#123456;" in css and "--font-heading:'Inter', sans-serif;" in css
    assert f"--accent:{DeckTheme().accent};" in css and "--accent-2:" in css and "--font-body:" in css
    assert theme_css({}) == theme_css(DeckTheme().model_dump())


def test_theme_css_rejects_values_that_could_break_out():
    default = DeckTheme().background
    for bad in ("#fff;}body{display:none", "url(https://evil/x)", "red</style><script>", "rgb(1,2", "a\\3C", 12):
        assert f"--bg:{default};" in theme_css({"background": bad}), bad
    assert "--bg:linear-gradient(135deg, #0f172a 0%, rgb(30 41 59 / 80%) 100%);" in theme_css(
        {"background": "linear-gradient(135deg, #0f172a 0%, rgb(30 41 59 / 80%) 100%)"}
    )


# --- slide document --------------------------------------------------------------------------


def test_slide_document_has_theme_stage_rules_and_sanitised_fragment():
    doc = render_slide_document('<p class="x">hi</p><script>alert(1)</script>', {"background": "#000"})
    assert doc.startswith("<!doctype html>")
    assert ":root{--bg:#000;" in doc
    assert (
        f"html,body{{width:{STAGE_WIDTH}px;height:{STAGE_HEIGHT}px;margin:0;overflow:hidden;"
        "background:var(--bg);color:var(--text);font-family:var(--font-body)}" in doc
    )
    assert '<p class="x">hi</p>' in doc and "alert" not in doc
    metas = outline(doc).attrs("meta")
    assert any(m.get("http-equiv") == "Content-Security-Policy" and m.get("content") == slide_csp() for m in metas)
    assert len(outline(doc).attrs("script")) == 0


def test_slide_csp_allows_no_script_at_all():
    csp = slide_csp()
    assert csp.startswith("default-src 'none'") and "script-src" not in csp
    assert "style-src 'unsafe-inline'" in csp and "img-src https: data:" in csp and "base-uri 'none'" in csp


# --- deck document ---------------------------------------------------------------------------


def test_deck_has_one_sandboxed_srcdoc_iframe_per_slide_in_order():
    doc = render_deck_document("Deck", {}, SLIDES, nonce="abc", start_index=1)
    frames = outline(doc).attrs("iframe")
    assert [f["data-slide-id"] for f in frames] == ["sld_1", "sld_2", "sld_3"]
    assert all(f["sandbox"] == "" and f["tabindex"] == "-1" for f in frames)
    assert [f.get("class") for f in frames] == [None, "current", None]
    assert [f["aria-hidden"] for f in frames] == ["true", "false", "true"]
    # Each srcdoc is a full slide document carrying the theme and the sanitised fragment.
    for frame, slide in zip(frames, SLIDES, strict=True):
        inner = frame["srcdoc"]
        assert inner.startswith("<!doctype html>") and ":root{--bg:" in inner
        assert inner == render_slide_document(slide["html"], {})
    assert frames[0]["srcdoc"].count("<p>one</p>") == 1
    assert 'viewBox="0 0 1 1"' in frames[2]["srcdoc"]


def test_deck_escapes_hostile_titles_and_slide_html():
    doc = render_deck_document(HOSTILE_TITLE, {}, SLIDES, nonce="abc")
    parsed = outline(doc)
    # Exactly one script element: ours, with the nonce. Everything else is text or attribute content.
    assert len(parsed.attrs("script")) == 1 and parsed.attrs("script")[0]["nonce"] == "abc"
    assert doc.count("<script") == 1 and doc.count("</script>") == 1
    assert doc.count("<iframe") == len(SLIDES) and doc.count("</iframe>") == len(SLIDES)
    assert "alert" not in parsed.scripts[0]
    assert parsed.title == HOSTILE_TITLE  # round-trips as text, never as markup
    assert f"<title>{html.escape(HOSTILE_TITLE, quote=False)}</title>" in doc
    hostile_frame = parsed.attrs("iframe")[1]
    assert hostile_frame["title"] == f"Slide 2: {HOSTILE_TITLE}"
    inner = hostile_frame["srcdoc"]
    assert "Hi \"there\" &amp; 'you'" in inner and 'title="a&quot;b"' in inner
    assert "<style>p{color:red}</style>" in inner
    assert "<script" not in inner and "onclick" not in inner and "<p>tail</p>" in inner


def test_deck_csp_and_nonce_are_consistent():
    nonce = "x-Y_z0123456789abcdef"
    doc = render_deck_document("Deck", {}, SLIDES, nonce=nonce)
    csp = deck_csp(nonce)
    assert f"script-src 'nonce-{nonce}'" in csp
    assert csp.startswith("default-src 'none'") and "img-src https: data:" in csp and "style-src 'unsafe-inline'" in csp
    assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0] and "'unsafe-eval'" not in csp
    metas = outline(doc).attrs("meta")
    assert any(m.get("http-equiv") == "Content-Security-Policy" and m.get("content") == csp for m in metas)
    assert f'<script nonce="{nonce}">' in doc


def test_deck_script_is_static_and_speaks_the_documented_protocol():
    a = outline(render_deck_document("A", {}, SLIDES, nonce="1")).scripts[0]
    other = render_deck_document(HOSTILE_TITLE, {"accent": "#f00"}, SLIDES[:1], nonce="2", controls=False)
    b = outline(other).scripts[0]
    assert a == b == "\n" + deck_render._DECK_SCRIPT  # configuration travels in data-* attributes only
    for token in ("evently:goto", "evently:ready", "evently:slide", "slideCount", "event.source !== window.parent"):
        assert token in a
    assert str(STAGE_WIDTH) in a and str(STAGE_HEIGHT) in a and "%(" not in a


def test_controls_flag_and_start_index_travel_in_data_attributes():
    doc = render_deck_document("Deck", {}, SLIDES, nonce="n", start_index=2, controls=False)
    stage = outline(doc).attrs("main")[0]
    assert stage["data-start"] == "2" and stage["data-controls"] == "false"
    assert 'id="counter"' not in doc
    doc = render_deck_document("Deck", {}, SLIDES, nonce="n", start_index=99, controls=True)
    stage = outline(doc).attrs("main")[0]
    assert stage["data-start"] == "2" and stage["data-controls"] == "true"  # clamped
    assert 'id="counter"' in doc


def test_stage_is_fixed_size_scaled_and_hidden_slides_stay_loaded():
    doc = render_deck_document("Deck", {}, SLIDES, nonce="n")
    css = re.search(r"<style>(.*?)</style>", doc, re.DOTALL).group(1)
    assert f"#stage{{position:absolute;left:50%;top:50%;width:{STAGE_WIDTH}px;height:{STAGE_HEIGHT}px;" in css
    assert "transform-origin:50% 50%" in css and "touch-action:none" in css
    assert "#stage iframe{" in css and "visibility:hidden;pointer-events:none}" in css
    assert "#stage iframe.current{visibility:visible}" in css
    assert "display:none" not in css
    assert '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">' in doc


def test_empty_deck_renders():
    doc = render_deck_document("Empty", {}, [], nonce="n", start_index=3)
    parsed = outline(doc)
    assert parsed.attrs("iframe") == [] and parsed.attrs("main")[0]["data-start"] == "0"
