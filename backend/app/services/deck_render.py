"""Turns stored slides into browser-ready HTML documents.

Slide HTML contract (shared by the slide-render agents and this renderer):

* A slide is an HTML *fragment* for a fixed 1280x720 px stage. It may contain one
  <style> block, inline SVG and https/data images. No scripts, no external CSS or fonts.
* Each slide is displayed alone inside its own sandboxed iframe document, so a slide's
  CSS can never collide with another slide's.
* That document defines the deck theme as CSS variables on :root, and slides should use
  them so a theme change restyles the deck without re-rendering:
      --bg --surface --text --muted --accent --accent-2 --font-heading --font-body
  It also sets `html, body {width:1280px; height:720px; margin:0; overflow:hidden;
  background:var(--bg); color:var(--text); font-family:var(--font-body)}`.

Security: slide HTML is untrusted model output. It is sanitised again here (so the
renderer is safe even for rows written without `sanitize_slide_html`), shown only in
`<iframe sandbox srcdoc>` frames (no scripts, opaque origin, no navigation) and every
document carries a Content-Security-Policy, both as the HTTP header the API sends and as
a <meta> tag so an exported file keeps the same policy.
"""

from __future__ import annotations

import html
import re
from typing import Any

from app.schemas.presentation import DeckTheme
from app.services.html_sanitize import sanitize_slide_html

STAGE_WIDTH = 1280
STAGE_HEIGHT = 720

# DeckTheme field -> CSS variable
_THEME_VARIABLES = (
    ("background", "--bg"),
    ("surface", "--surface"),
    ("text", "--text"),
    ("muted", "--muted"),
    ("accent", "--accent"),
    ("accent_2", "--accent-2"),
    ("heading_font", "--font-heading"),
    ("body_font", "--font-body"),
)
# Colours, gradients and font stacks only; no `;`, `{`, `}`, `<`, `\`, `:` or `url(`.
_SAFE_CSS_VALUE = re.compile(r"[\w #(),.%/'\"+-]+")
_DEFAULT_THEME = DeckTheme().model_dump()

_STAGE_RULES = (
    f"html,body{{width:{STAGE_WIDTH}px;height:{STAGE_HEIGHT}px;margin:0;overflow:hidden;"
    "background:var(--bg);color:var(--text);font-family:var(--font-body)}"
)
_RESOURCE_POLICY = "default-src 'none'; style-src 'unsafe-inline'; img-src https: data:; font-src data:"
_NAVIGATION_POLICY = "base-uri 'none'; form-action 'none'"


def slide_csp() -> str:
    """CSP for a single slide document: styles and images only, no script of any kind."""
    return f"{_RESOURCE_POLICY}; {_NAVIGATION_POLICY}"


def deck_csp(nonce: str) -> str:
    """CSP for the deck document: additionally allows the one navigation script carrying `nonce`.

    Slides embedded via `srcdoc` inherit this policy, so they can never run script either.
    """
    return f"{_RESOURCE_POLICY}; script-src 'nonce-{nonce}'; {_NAVIGATION_POLICY}"


def _safe_css_value(value: Any, default: str) -> str:
    if not isinstance(value, str) or not _SAFE_CSS_VALUE.fullmatch(value) or "url(" in value.lower():
        return default
    if value.count("(") != value.count(")") or value.count('"') % 2 or value.count("'") % 2:
        return default
    return value


def theme_css(theme: dict[str, Any]) -> str:
    """`:root{...}` block mapping a `DeckTheme` dict to the CSS variables above.

    Missing or unsafe values fall back to the `DeckTheme` defaults, so theme values written
    by an agent or a PATCH can never break out of the declaration block.
    """
    theme = theme or {}
    declarations = "".join(
        f"{variable}:{_safe_css_value(theme.get(field), _DEFAULT_THEME[field])};"
        for field, variable in _THEME_VARIABLES
    )
    return f":root{{{declarations}}}"


def _attr(value: str) -> str:
    return html.escape(value, quote=True)


def render_slide_document(slide_html: str, theme: dict[str, Any]) -> str:
    """Complete standalone HTML document showing one (sanitised) slide on the 1280x720 stage."""
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{_attr(slide_csp())}">\n'
        f'<meta name="viewport" content="width={STAGE_WIDTH}">\n'
        f"<style>{theme_css(theme)}{_STAGE_RULES}</style>\n"
        "</head><body>\n"
        f"{sanitize_slide_html(slide_html)}\n"
        "</body></html>"
    )


_DECK_CSS = (
    "html,body{height:100%;margin:0;overflow:hidden;background:var(--bg)}"
    "body{touch-action:none;-webkit-user-select:none;user-select:none;-webkit-tap-highlight-color:transparent}"
    f"#stage{{position:absolute;left:50%;top:50%;width:{STAGE_WIDTH}px;height:{STAGE_HEIGHT}px;"
    f"margin:-{STAGE_HEIGHT // 2}px 0 0 -{STAGE_WIDTH // 2}px;transform-origin:50% 50%;background:var(--bg)}}"
    # Hidden slides stay loaded (visibility, not display) so switching is instant; frames never take
    # pointer events or focus, so taps and keys always reach the deck itself.
    "#stage iframe{position:absolute;left:0;top:0;width:100%;height:100%;border:0;background:var(--bg);"
    "visibility:hidden;pointer-events:none}"
    "#stage iframe.current{visibility:visible}"
    "#counter{position:fixed;right:12px;bottom:10px;padding:2px 8px;border-radius:10px;"
    "font:12px/18px system-ui,sans-serif;color:#fff;background:rgba(0,0,0,.45);pointer-events:none}"
)

# Static on purpose: configuration travels in data-* attributes, so nothing is ever interpolated into script.
_DECK_SCRIPT = """\
(function () {
  'use strict';
  var stage = document.getElementById('stage');
  var frames = Array.prototype.slice.call(stage.querySelectorAll('iframe'));
  var counter = document.getElementById('counter');
  var controls = stage.getAttribute('data-controls') === 'true';
  var count = frames.length;
  var current = -1;

  function post(message) {
    if (window.parent && window.parent !== window) {
      try { window.parent.postMessage(message, '*'); } catch (e) { /* no embedder */ }
    }
  }
  function show(index) {
    if (!count) { return; }
    index = Math.max(0, Math.min(count - 1, index));
    if (index === current) { return; }
    frames.forEach(function (frame, i) {
      frame.classList.toggle('current', i === index);
      frame.setAttribute('aria-hidden', i === index ? 'false' : 'true');
    });
    current = index;
    if (counter) { counter.textContent = (index + 1) + ' / ' + count; }
    post({ type: 'evently:slide', index: index });
  }
  function next() { show(current + 1); }
  function prev() { show(current - 1); }
  function fit() {
    var scale = Math.min(window.innerWidth / STAGE_WIDTH, window.innerHeight / STAGE_HEIGHT);
    stage.style.transform = 'scale(' + scale + ')';
  }

  window.addEventListener('resize', fit);
  window.addEventListener('message', function (event) {
    if (event.source !== window.parent) { return; }
    var data = event.data;
    if (!data || data.type !== 'evently:goto' || typeof data.index !== 'number' || !isFinite(data.index)) { return; }
    show(Math.round(data.index));
  });

  if (controls) {
    var NEXT = ['ArrowRight', 'ArrowDown', 'PageDown', ' ', 'Enter', 'n', 'j'];
    var PREV = ['ArrowLeft', 'ArrowUp', 'PageUp', 'Backspace', 'p', 'k'];
    window.addEventListener('keydown', function (event) {
      if (event.metaKey || event.ctrlKey || event.altKey) { return; }
      if (NEXT.indexOf(event.key) >= 0) { next(); }
      else if (PREV.indexOf(event.key) >= 0) { prev(); }
      else if (event.key === 'Home') { show(0); }
      else if (event.key === 'End') { show(count - 1); }
      else { return; }
      event.preventDefault();
    });
    var down = null;
    document.addEventListener('pointerdown', function (event) { down = { x: event.clientX, y: event.clientY }; });
    document.addEventListener('pointercancel', function () { down = null; });
    document.addEventListener('pointerup', function (event) {
      if (!down) { return; }
      var dx = event.clientX - down.x, dy = event.clientY - down.y;
      down = null;
      if (Math.abs(dx) > 40 && Math.abs(dx) > Math.abs(dy)) {
        if (dx < 0) { next(); } else { prev(); }
      } else if (Math.abs(dx) < 10 && Math.abs(dy) < 10) {
        if (event.clientX > window.innerWidth / 3) { next(); } else { prev(); }
      }
    });
  }

  fit();
  var start = Math.max(0, Math.min(count - 1, parseInt(stage.getAttribute('data-start'), 10) || 0));
  frames.forEach(function (frame, i) { frame.classList.toggle('current', i === start); });
  current = start;
  if (counter && count) { counter.textContent = (start + 1) + ' / ' + count; }
  post({ type: 'evently:ready', slideCount: count, index: count ? start : -1 });
})();
""".replace("STAGE_WIDTH", str(STAGE_WIDTH)).replace("STAGE_HEIGHT", str(STAGE_HEIGHT))


def render_deck_document(
    title: str,
    theme: dict[str, Any],
    slides: list[dict[str, Any]],
    *,
    nonce: str,
    start_index: int = 0,
    controls: bool = True,
) -> str:
    """Complete HTML deck: every slide in its own sandboxed iframe, scaled to fit the viewport.

    slides: dicts with at least `id`, `title`, `html`, in display order.
    nonce: CSP nonce applied to the single inline navigation script.

    The deck speaks a small postMessage protocol with its embedding page:
      in :  {"type": "evently:goto", "index": n}
      out:  {"type": "evently:ready", "slideCount": n}, {"type": "evently:slide", "index": n}
    `evently:ready` also carries the starting `index`; `evently:slide` is sent whenever the
    visible slide changes, whatever caused it (keys, taps, swipes or a goto). Only messages
    from the parent window are honoured. With `controls` false the deck has no keyboard,
    tap or swipe handling and no counter: it just follows `evently:goto`.
    """
    start_index = max(0, min(len(slides) - 1, start_index)) if slides else 0
    frames = []
    for index, slide in enumerate(slides):
        label = f"Slide {index + 1}: {slide.get('title') or 'Untitled'}"
        current = ' class="current"' if index == start_index else ""
        frames.append(
            f'<iframe sandbox="" srcdoc="{_attr(render_slide_document(slide.get("html") or "", theme))}"'
            f' title="{_attr(label)}" data-slide-id="{_attr(str(slide.get("id", "")))}" data-index="{index}"'
            f'{current} aria-hidden="{"false" if index == start_index else "true"}" tabindex="-1"></iframe>'
        )
    counter = '<div id="counter" aria-live="polite"></div>' if controls else ""
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{_attr(deck_csp(nonce))}">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        f"<title>{html.escape(title, quote=False)}</title>\n"
        f"<style>{theme_css(theme)}{_DECK_CSS}</style>\n"
        "</head><body>\n"
        f'<main id="stage" data-start="{start_index}" data-controls="{"true" if controls else "false"}"'
        ' aria-roledescription="slide deck">\n'
        + "\n".join(frames)
        + "\n</main>\n"
        f"{counter}\n"
        f'<script nonce="{_attr(nonce)}">\n{_DECK_SCRIPT}</script>\n'
        "</body></html>"
    )
