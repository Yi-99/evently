"""Slide HTML sanitiser: active content is removed, honest slides survive."""

import random
import re
from html.parser import HTMLParser

import pytest

from app.services.html_sanitize import sanitize_slide_html as sanitize

_FORBIDDEN_TAGS = {
    "script", "iframe", "object", "embed", "link", "meta", "base", "form", "frame", "frameset", "applet",
    "template", "noscript", "noembed", "noframes", "xmp", "plaintext", "portal",
}  # fmt: skip
_FORBIDDEN_CSS = re.compile(
    r"@import\b|expression\s*\(|behavior\s*:|-moz-binding\s*:|url\(\s*['\"]?\s*(?:http:|//|javascript:|/|['\"]?\s*\))",
    re.IGNORECASE,
)


class _Audit(HTMLParser):
    """Re-parses sanitiser output the way a browser would and records anything active."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.problems: list[str] = []
        self._style: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag in _FORBIDDEN_TAGS:
            self.problems.append(f"<{tag}>")
        for name, value in attrs:
            flat = re.sub(r"[\s\x00-\x1f]", "", value or "").lower()
            if name.startswith("on") or name in ("srcdoc", "srcset", "ping"):
                self.problems.append(f"{name}=")
            if flat.startswith(("javascript:", "vbscript:", "data:text", "data:application")):
                self.problems.append(f"{name}={value!r}")
            if name == "style" and _FORBIDDEN_CSS.search(value or ""):
                self.problems.append(f"style={value!r}")
        if tag == "style":
            self._style = ""

    handle_startendtag = handle_starttag

    def handle_endtag(self, tag):
        if tag == "style" and self._style is not None:
            if _FORBIDDEN_CSS.search(self._style) or "<" in self._style:
                self.problems.append(f"<style>{self._style!r}")
            self._style = None

    def handle_data(self, data):
        if self._style is not None:
            self._style += data


def assert_clean(output: str) -> None:
    audit = _Audit()
    audit.feed(output)
    audit.close()
    assert not audit.problems, f"{audit.problems} survived in {output!r}"


def check(html: str) -> str:
    """Sanitise, assert the result is clean and idempotent, return it."""
    out = sanitize(html)
    assert_clean(out)
    assert sanitize(out) == out, "sanitiser is not idempotent"
    return out


# --- a realistic slide survives ------------------------------------------------------------

REALISTIC_SLIDE = """<style>
  .slide { display: grid; grid-template-columns: 1fr 1fr; gap: 48px; padding: 64px; height: 720px; }
  h1 { font-family: var(--font-heading); color: var(--accent); font-size: 56px; margin: 0 0 16px; }
  .card { background: var(--surface); border-radius: 16px; padding: 24px; }
  .card::before { content: "\\201C"; color: var(--muted); }
  .photo { background: url("https://images.example.com/venue.jpg") center/cover; }
  @media (max-width: 1280px) { h1 { font-size: 48px; } }
</style>
<div class="slide">
  <div>
    <h1>The Riverside Loft</h1>
    <p class="lede">Capacity <strong>180</strong> &middot; 12 min from downtown &amp; 2 parking garages</p>
    <ul>
      <li>Rooftop terrace with skyline views</li>
      <li>In-house AV &mdash; no rental needed</li>
    </ul>
    <svg viewBox="0 0 200 100" width="200" height="100" role="img" aria-label="Capacity chart">
      <defs>
        <linearGradient id="g1" x1="0" y1="0" x2="1" y2="0" gradientUnits="objectBoundingBox">
          <stop offset="0%" stop-color="var(--accent)"/>
          <stop offset="100%" stop-color="var(--accent-2)"/>
        </linearGradient>
        <clipPath id="c1"><rect x="0" y="0" width="200" height="100" rx="8"/></clipPath>
      </defs>
      <g clip-path="url(#c1)">
        <rect x="10" y="20" width="140" height="30" fill="url(#g1)" stroke="var(--muted)" stroke-width="2"/>
        <text x="20" y="40" fill="var(--text)" font-family="var(--font-body)" font-size="14">180 seats</text>
        <path d="M10 80 L190 80" stroke="var(--muted)" stroke-dasharray="4 2"/>
        <circle cx="160" cy="35" r="6" fill="var(--accent-2)">
          <animate attributeName="r" values="6;9;6" dur="2s" repeatCount="indefinite"/>
        </circle>
      </g>
    </svg>
  </div>
  <div class="card photo" style="min-height: 400px; background-color: var(--surface)">
    <img src="https://images.example.com/floorplan.png" alt="Floor plan &quot;A&quot;" width="400" height="300">
    <img src="data:image/png;base64,iVBORw0KGgo=" alt="">
    <a href="https://example.com/venue" target="_blank" rel="noopener">Venue page</a>
  </div>
</div>"""


def test_realistic_slide_keeps_its_meaning():
    out = check(REALISTIC_SLIDE)
    # Structure, text and styles are all still there.
    for fragment in (
        "<h1>The Riverside Loft</h1>",
        "Capacity <strong>180</strong> · 12 min from downtown &amp; 2 parking garages",
        "In-house AV — no rental needed",
        'content: "\\201C"',
        'background: url("https://images.example.com/venue.jpg") center/cover',
        "@media (max-width: 1280px)",
        'viewBox="0 0 200 100"',
        'gradientUnits="objectBoundingBox"',
        '<linearGradient id="g1"',
        '<clipPath id="c1">',
        'clip-path="url(#c1)"',
        'fill="url(#g1)"',
        '<animate attributeName="r" values="6;9;6" dur="2s" repeatCount="indefinite" />',
        '<stop offset="0%" stop-color="var(--accent)" />',
        'style="min-height: 400px; background-color: var(--surface)"',
        '<img src="https://images.example.com/floorplan.png" alt="Floor plan &quot;A&quot;" width="400" height="300">',
        '<img src="data:image/png;base64,iVBORw0KGgo=" alt="">',
        '<a href="https://example.com/venue" target="_blank" rel="noopener">Venue page</a>',
    ):
        assert fragment in out, fragment
    assert out.count("<svg") == 1 and out.count("</svg>") == 1 and out.count("<style>") == 1


def test_empty_and_plain_text():
    assert sanitize("") == ""
    assert sanitize("Just text & <3") == "Just text &amp; &lt;3"


# --- elements ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        "<script>alert(1)</script>",
        "<SCRIPT SRC=https://evil/x.js></SCRIPT>",
        "<script\n>alert(1)</script\t>",
        "<script/>alert(1)</script>",  # the slash does not close a script in HTML
        "<script>alert(1)",  # never closed
        "<iframe src='https://evil'></iframe>",
        "<iframe srcdoc='<script>alert(1)</script>'>",
        "<object data='https://evil/x.swf'></object>",
        "<embed src='https://evil/x.svg'>",
        "<link rel=stylesheet href=https://evil/x.css>",
        "<link rel=import href=https://evil/x.html>",
        "<meta http-equiv=refresh content='0;url=https://evil'>",
        "<base href='https://evil/'>",
        "<form action='https://evil'><input autofocus onfocus=alert(1)></form>",
        "<template><img src=x onerror=alert(1)></template>",
        "<noscript><p title='</noscript><img src=x onerror=alert(1)>'></noscript>",
        "<xmp><img src=x onerror=alert(1)></xmp>",
        "<noembed><img src=x onerror=alert(1)></noembed>",
        "<frameset><frame src='https://evil'></frameset>",
        "<plaintext><img src=x onerror=alert(1)>",
        "<svg><script>alert(1)</script></svg>",
        "<svg><script href='https://evil/x.js'/></svg>",
        "<math><mi><script>alert(1)</script></mi></math>",
    ],
)
def test_active_elements_are_removed_with_contents(payload):
    out = check(f"<p>before</p>{payload}<p>after</p>")
    assert "alert" not in out and "evil" not in out
    assert out.startswith("<p>before</p>")


def test_removed_elements_take_their_contents_not_their_siblings():
    out = check("<p>a</p><form><p>inside</p></form><p>b</p>")
    assert out == "<p>a</p><p>b</p>"


def test_nested_removed_elements_are_tracked():
    out = check("<object><object></object><p>still inside</p></object><p>out</p>")
    assert out == "<p>out</p>"


def test_comments_doctype_and_processing_instructions_are_dropped():
    out = check("<!DOCTYPE html><!-- hi --><?xml version='1.0'?><p>x</p><!--[if IE]><script>a()</script><![endif]-->")
    assert out == "<p>x</p>"


def test_text_content_is_escaped_not_interpreted():
    out = check("<p>1 < 2 && 3 > 2</p><textarea><img src=x onerror=1></textarea>")
    assert out == "<p>1 &lt; 2 &amp;&amp; 3 &gt; 2</p><textarea>&lt;img src=x onerror=1&gt;</textarea>"


# --- attributes --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        '<img src="https://ok/x.png" onerror="alert(1)">',
        '<img src="https://ok/x.png" ONERROR="alert(1)">',
        "<img src=x onerror=alert(1)>",
        "<div onmouseover=alert(1) onclick='x'>x</div>",
        "<body onload=alert(1)>",
        "<svg onload=alert(1)></svg>",
        "<svg><g onactivate=alert(1)/></svg>",
        '<img src="https://ok/x.png"\nonerror\n=\n"alert(1)">',
        "<input autofocus onfocus=alert(1)>",
        '<a href="https://ok" onclick="alert(1)">x</a>',
        "<img/src=x/onerror=alert(1)>",
    ],
)
def test_event_handlers_are_removed(payload):
    out = check(payload)
    assert "alert" not in out


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "  javascript:alert(1)",
        "\tjavascript:alert(1)",
        "java\tscript:alert(1)",
        "java\nscript:alert(1)",
        "java\rscript:alert(1)",
        "java\x01script:alert(1)",
        "&#106;avascript:alert(1)",
        "&#x6A;avascript:alert(1)",
        "&#106avascript:alert(1)",
        "&#0000106;avascript:alert(1)",
        "&Tab;javascript:alert(1)",
        "jav&#x09;ascript:alert(1)",
        "vbscript:msgbox(1)",
        "data:text/html,<script>alert(1)</script>",
        "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
        "DATA:text/html,x",
        "data:application/xhtml+xml,x",
        "//evil.example/x",
        "/relative",
        "relative.html",
        "ftp://evil/x",
        "blob:https://evil/uuid",
        "file:///etc/passwd",
    ],
)
def test_dangerous_urls_are_dropped_from_every_url_attribute(url):
    for template in (
        '<a href="{u}">x</a>',
        "<a href={u}>x</a>",
        '<img src="{u}">',
        '<svg><a xlink:href="{u}"><text>x</text></a></svg>',
        '<svg><a href="{u}"><text>x</text></a></svg>',
        '<svg><image href="{u}"/></svg>',
        '<svg><image xlink:href="{u}"/></svg>',
        '<button formaction="{u}">x</button>',
        '<video poster="{u}"></video>',
        '<svg><use href="{u}"/></svg>',
    ):
        out = check(template.format(u=url.replace('"', "&quot;")))
        assert not re.search(r'(?:href|src|formaction|poster)=', out), (template, url, out)


@pytest.mark.parametrize(
    "html",
    [
        '<a href="https://example.com/x?y=1&amp;z=2">x</a>',
        '<a href="HTTPS://EXAMPLE.COM">x</a>',
        '<a href="http://example.com">x</a>',
        '<a href="mailto:a@example.com">x</a>',
        '<a href="tel:+15550100">x</a>',
        '<a href="#section">x</a>',
        '<img src="https://example.com/x.png">',
        '<img src="data:image/png;base64,AAAA">',
        '<img src="data:image/svg+xml;utf8,&lt;svg/&gt;">',
        '<img src="data:image/jpeg;base64,/9j/">',
        '<img src="data:image/webp;base64,UklGR">',
        '<svg><image href="https://example.com/x.png" /></svg>',
        '<svg><use href="#icon" /></svg>',
        '<svg><use xlink:href="#icon" /></svg>',
        '<svg><textPath href="#curve">t</textPath></svg>',
    ],
)
def test_safe_urls_survive(html):
    out = check(html)
    assert re.search(r'(?:href|src)="', out), out
    assert sanitize(html).count("=") >= 1


def test_image_data_url_allowed_on_img_but_not_as_link():
    assert 'src="data:image/png' in check('<img src="data:image/png;base64,AAAA">')
    assert "href" not in check('<a href="data:image/png;base64,AAAA">x</a>')


def test_svg_use_only_accepts_same_document_references():
    assert "href" not in check('<svg><use href="https://evil.example/sprite.svg#icon"/></svg>')
    assert "href" not in check('<svg><use xlink:href="data:image/svg+xml;base64,PHN2Zz4="/></svg>')
    assert 'href="#icon"' in check('<svg><use href="#icon"/></svg>')


def test_srcdoc_srcset_and_ping_are_dropped():
    out = check('<img src="https://ok/x.png" srcset="//evil/x.png 2x" ping="https://evil/p" srcdoc="x">')
    assert out == '<img src="https://ok/x.png">'


def test_first_duplicate_attribute_wins_like_a_browser():
    assert check('<div class="a" class="b">x</div>') == '<div class="a">x</div>'


def test_attribute_values_are_escaped_and_quoted():
    out = check("<div title='a\"b<c>d' data-x=\"it's\">x</div>")
    assert out == '<div title="a&quot;b&lt;c&gt;d" data-x="it&#x27;s">x</div>'


def test_weird_attribute_and_tag_names_are_dropped():
    out = check('<div "onclick"="alert(1)" =x>y</div>')
    assert out == "<div>y</div>"
    out = check("<a<script>alert(1)</script>>t")
    assert "<a" not in out and "<script" not in out and "</script>" not in out


# --- SVG tricks --------------------------------------------------------------------------------


def test_svg_case_sensitive_names_and_self_closing_tags_are_preserved():
    out = check(
        '<svg viewBox="0 0 10 10" preserveAspectRatio="xMidYMid"><radialGradient id="r" gradientTransform="x"/>'
        '<feGaussianBlur stdDeviation="2"/><foreignObject width="10" height="10"><p>hi</p></foreignObject></svg>'
    )
    assert out == (
        '<svg viewBox="0 0 10 10" preserveAspectRatio="xMidYMid"><radialGradient id="r" gradientTransform="x" />'
        '<feGaussianBlur stdDeviation="2" /><foreignObject width="10" height="10"><p>hi</p></foreignObject></svg>'
    )


def test_foreign_object_contents_are_sanitised():
    out = check(
        "<svg><foreignObject><iframe srcdoc='<script>alert(1)</script>'></iframe>"
        "<body onload='alert(1)'><p>ok</p><script>alert(2)</script></foreignObject></svg>"
    )
    assert "alert" not in out and "<p>ok</p>" in out


def test_svg_animations_cannot_target_hrefs_or_handlers():
    out = check(
        '<svg><a href="#x"><rect width="10" height="10"/>'
        '<animate attributeName="href" values="javascript:alert(1)" begin="0s"/>'
        '<set attributeName="xlink:href" to="javascript:alert(1)"/>'
        '<animate attributeName="ONLOAD" to="alert(1)"/>'
        '<animate attributeName="width" values="10;20;10" dur="1s"/></a></svg>'
    )
    assert "alert" not in out
    assert '<animate attributeName="width" values="10;20;10" dur="1s" />' in out


def test_script_scheme_is_dropped_from_unknown_attributes_too():
    out = check('<svg><set attributeName="x" to="javascript:alert(1)" from="&#106;avascript:1"/></svg>')
    assert out == '<svg><set attributeName="x" /></svg>'


def test_svg_style_block_cannot_smuggle_markup():
    # In HTML a <style> is raw text, inside <svg> its content is parsed as markup. Neutralising `<`
    # keeps the block inert under both readings.
    out = check("<svg><style><img src=x onerror=alert(1)></style></svg>")
    assert "<img" not in out and out.startswith("<svg><style>")


def test_svg_cdata_sections_become_text_and_style_cdata_wrappers_are_dropped():
    assert check("<svg><text><![CDATA[a <b> c]]></text></svg>") == "<svg><text>a &lt;b&gt; c</text></svg>"
    assert check("<style><![CDATA[ p{color:red} ]]></style>") == "<style> p{color:red} </style>"


# --- CSS ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "css",
    [
        "@import url(https://evil/x.css);",
        "@import 'https://evil/x.css';",
        '@IMPORT "x.css" screen;',
        "@im\\70ort url(x);",
        "@\\69 mport url(x);",
        "@im/**/port url(x);",  # not an @import; must not become one
        "@im@import x;port url(http://evil);",  # removal must not create a new @import
        "body{background:url(javascript:alert(1))}",
        "body{background:url('javascript:alert(1)')}",
        'body{background:url("  javascript:alert(1)")}',
        "body{background:url(http://evil/x.png)}",
        "body{background:url(//evil/x.png)}",
        "body{background:url(/relative.png)}",
        "body{background:URL(HTTP://evil/x.png)}",
        "body{background:\\75rl(http://evil/x.png)}",
        "body{background:u\\52 l(http://evil/x.png)}",
        "body{background:url(\\68ttp://evil/x.png)}",
        "body{background:url(ja\\76 ascript:alert(1))}",
        "body{background:url()}",
        "body{width:expression(alert(1))}",
        "body{width:EXPRESSION (alert(1))}",
        "body{width:expr\\65ssion(alert(1))}",
        "body{behavior:url(https://evil/x.htc)}",
        "body{-moz-binding:url(https://evil/x.xml#a)}",
        "u@import x;rl(http://evil)",  # removal must not create a url(...)
    ],
)
def test_dangerous_css_is_neutralised_in_style_blocks_and_attributes(css):
    out = check(f"<style>{css}</style><div style=\"{css.replace('\"', '&quot;')}\">x</div>")
    assert "@import" not in out and "javascript:" not in out.lower()


def test_neutralised_css_leaves_inert_declarations_behind():
    assert check("<style>a{width:expression(alert(1))}</style>") == "<style>a{width: (alert(1))}</style>"
    assert check("<style>a{behavior:url(https://x/y.htc)}</style>") == "<style>a{-x-removed:url(https://x/y.htc)}</style>"
    assert check("<style>a{background:url(http://x/y.png) no-repeat}</style>") == (
        "<style>a{background:none no-repeat}</style>"
    )
    assert check("<style>@import url(x);a{color:red}</style>") == "<style> a{color:red}</style>"


def test_safe_css_survives_untouched():
    css = (
        "@font-face{font-family:X;src:url(data:font/woff2;base64,AAAA)}"
        ".a{background:url(https://ok/x.png) no-repeat,url('https://ok/y.png');fill:url(#grad)}"
        ".b::after{content:\"\\201C\";color:#fff}.c\\:d{color:red}@media (min-width:600px){.e{width:50%}}"
        "@supports (display:grid){.f{display:grid}}.g{transform:rotate(45deg) translate(1px,2px)}"
    )
    assert check(f"<style>{css}</style>") == f"<style>{css}</style>"


def test_style_content_never_contains_a_tag_opener():
    out = check("<style>a{b:c}</style><svg><style>x{}</style><style>y{}</style></svg>")
    for block in re.findall(r"<style>(.*?)</style>", out, re.DOTALL):
        assert "<" not in block


def test_svg_presentation_attributes_go_through_the_url_filter():
    out = check('<svg><rect fill="url(http://evil/x.svg#a)" filter="url(#f)" stroke="url(\\68ttp://evil)"/></svg>')
    assert out == '<svg><rect fill="none" filter="url(#f)" stroke="none" /></svg>'


def test_self_closed_style_in_html_is_not_an_open_raw_text_element():
    # `<style/>` would otherwise make the browser treat the rest of the slide as unfiltered CSS.
    out = check("<style/>@import url(https://evil);</style><p>x</p>")
    assert out == "@import url(https://evil);<p>x</p>"  # plain text now, not a stylesheet


# --- robustness --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "html",
    [
        "<div><p>unclosed",
        "<img src=https://ok/x.png",
        "<a href='https://ok",
        "</p></div></html>",
        "<p>text</style></script>",
        "<![CDATA[x",
        "<!-- unterminated",
        "<?pi",
        "<!DOCTYPE",
        "<",
        "<<<>>>",
        "&#xZZ; &unknown; &amp",
        "<svg><![CDATA[",
        "<div/>text",
        "<br/><br></br>",
        "\x00<p>\x00nul\x00</p>",
    ],
)
def test_malformed_markup_never_raises_and_stays_clean(html):
    check(html)


def test_fail_closed_if_the_parser_blows_up(monkeypatch):
    from app.services import html_sanitize

    def boom(self, data):
        raise RuntimeError("parser bug")

    monkeypatch.setattr(html_sanitize._Sanitizer, "feed", boom)
    assert html_sanitize.sanitize_slide_html("<b>x</b>") == "&lt;b&gt;x&lt;/b&gt;"


def test_random_garbage_never_raises_and_stays_clean():
    rng = random.Random(1234)
    alphabet = list("<>/=\"' \n\tabcdefghijklmnopqrstuvwxyz0123456789&#;:()!-[]\\") + [
        "<script>", "</script>", "<svg>", "</svg>", "<style>", "</style>", "onerror=", "javascript:", "srcdoc=",
        "<img ", "<a ", "href=", "src=", "<!--", "-->", "<![CDATA[", "]]>", "url(", "@import", "<iframe>", "<form>",
        "<foreignObject>", "</foreignObject>", "<use ", "<title>", "</title>", "<textarea>", "<noscript>",
    ]  # fmt: skip
    for _ in range(400):
        html = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 60)))
        check(html)
