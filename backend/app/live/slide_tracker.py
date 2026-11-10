"""SlideTracker — decides when spoken words should move the deck.

Pure logic: no I/O, no clock unless `now_ms` is omitted. One tracker instance
follows one live session. A wrong slide change in front of an audience is far
worse than a missed one, so every rule below prefers precision to recall.

Signals, in priority order:
  1. Commands   "next slide", "previous slide", "go back", "go to slide five",
                "first slide", "last slide".
  2. Keywords   the speaker says a trigger keyword/phrase of the NEXT slide
                (keywords that also belong to the current slide are ignored).
  3. Script     the transcript has covered most of the current slide's notes
                and has started matching the next slide's notes.

The implementation must be robust to STT quirks: casing, punctuation, filler
words, numbers as words or digits, and interim results that repeat or revise
earlier text (a phrase must trigger at most once per utterance).

Input model
-----------
`feed` receives every STT result. Interim results are cumulative hypotheses for
the utterance in progress (text grows, earlier words may be revised); a final
closes the utterance and the next one starts from empty. The tracker keeps a
token stream of `carry + current utterance`, where `carry` is the tail of the
finished utterances, so phrases split across a final/interim boundary still
match. Two cursors guard "at most once":

* the *change point* — nothing starting before it may ever trigger again; it is
  set to the end of the triggering phrase after every change (automatic or
  `set_current`) and is re-anchored on the surrounding words when an interim
  revision shifts them;
* the *auto floor* — words heard during the automatic cooldown are dropped
  for keyword/script purposes rather than deferred, so a list of topics said
  quickly cannot cascade through several slides.

Command policy ("an instruction, not a mention")
------------------------------------------------
Commands are recognised from a fixed grammar; only the forms below count:

* explicit forms, which act immediately, even on an interim result:
  - a navigation verb aimed at a slide: go / move (on) / jump / skip / switch /
    advance / continue / proceed / flip / head / get / return / take us ...
    + "to" + "the next | previous | last | final | first slide", "slide N",
    "the Nth slide", "the beginning | start | end (of the deck)";
  - a request: "show (me/us) the next slide", "can/could we see slide five",
    "let's look at the first slide", "please next slide", "next slide please",
    "next please", "slide five please", "advance the slide", "skip this slide";
  - relative moves: "(go) back one slide", "go back two slides",
    "skip ahead a slide", "go one slide back". Relative moves clamp to the deck.
* bare forms: "next slide" / "previous slide" said without a verb. These are
  accepted only once the right-hand context is known: they wait for the
  following word (or the final result, or `command_settle_ms` of unchanged
  interims) and are rejected when that word turns the phrase into a noun
  phrase ("next slide shows ...", "next slide of ...", "next slide deck").
* "go back to the last slide" means the previous slide; "go to the last slide"
  means the end of the deck. "slide N" counts 1-based; out-of-range numbers are
  ignored. A go-to number that could still be extended ("slide twenty" with
  more than twenty slides) waits like a bare form.

Guards against ordinary speech that merely mentions a slide:

* adjacent word: the word right before the phrase must not attach it to a noun
  phrase or a narrative — determiners and prepositions ("the next slide",
  "on the next slide", "my last slide deck"), subject pronouns and auxiliaries
  ("you go to the next slide", "we went back one slide"), negations and verbs
  of mention ("don't go to", "say next slide"). A recognised lead-in
  ("let's", "can we", "please", "I'll", "ok so now") is absorbed first, so
  "ok, let's go to the next slide" passes while "then you go to the next
  slide" does not.
* clause: no subordinator or negation ("before", "when", "if", "once", "until",
  "unless", "while", "as we", "don't", "not", "never", "without", "instead") in
  the four words preceding the phrase within the same sentence and utterance.
  "Before we go to the next slide, any questions?" therefore never fires.
* "slide" is required: "go back", "next", "move on" alone are not commands
  (except "next please"). "I'll go back to that point later" is ignored.
* a rejected phrase also suppresses any shorter command nested inside it.

Commands have their own debounce (`command_debounce_ms`) instead of the
automatic cooldown: a duplicate within the debounce is dropped, a deliberate
"next slide ... next slide" a second later works.

Keywords and script
-------------------
A keyword of the next slide (or, with `allow_keyword_skip`, of any later slide,
nearest first) advances the deck. Matching is on token boundaries, tolerant of
split/joined compounds and of small recognition errors on long words; keywords
shared with the current slide, or contained in a longer keyword of the current
slide that the speaker actually said, are ignored, as are keywords made only
of stopwords. A keyword at the unstable tail of an interim result must be seen
twice (or be followed by another word, or be final) before it counts. Keywords
never move backwards and respect `cooldown_ms`.

Script mode follows the speaker through the current slide's notes by coverage
of their content stems (paraphrase, skipping and reordering are fine) and
advances once coverage reaches `script_coverage_threshold` and the latest
`script_lookahead_words` spoken words line up, in order, with the opening of
the next slide's notes — a run that also occurs in the current notes is not
evidence. Slides with empty notes never trigger by script, and a next slide
whose notes are shorter than the lookahead cannot be entered by script.
"""

from __future__ import annotations

import re
import time
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from app.live import text_match as tm

Mode = Literal["off", "commands", "keywords", "script"]


@dataclass
class SlideCue:
    index: int
    title: str = ""
    keywords: list[str] = field(default_factory=list)
    script: str = ""  # speaker notes


@dataclass
class TrackerConfig:
    mode: Mode = "keywords"
    cooldown_ms: int = 2500  # minimum gap between automatic (keyword/script) changes
    allow_keyword_skip: bool = False  # may a keyword jump further than the next slide?
    script_coverage_threshold: float = 0.6  # share of current notes that must be spoken
    script_lookahead_words: int = 4  # consecutive next-slide words needed to advance
    command_debounce_ms: int = 700  # a second command within this window is treated as an echo
    command_settle_ms: int = 600  # how long a bare "next slide" at the end of an interim waits


@dataclass
class SlideDecision:
    target_index: int
    reason: Literal["command", "keyword", "script"]
    matched: str  # the command, keyword or script phrase that triggered the change
    confidence: float = 1.0


# ---------------------------------------------------------------------------
# Command grammar
# ---------------------------------------------------------------------------

_CARRY_TOKENS = 8  # finished-utterance tokens kept as context for straddling phrases
_CLAUSE_WINDOW = 4
_OPENING_SLACK = 4  # the next notes' opening = lookahead + this many tokens
_ANCHOR_TAIL = 3
_REALIGN_RADIUS = 3

_NUM_WORD = "|".join(sorted(tm.NUMBER_WORDS, key=len, reverse=True))
_NUM_TOKEN = rf"(?:\d{{1,3}}(?:st|nd|rd|th)?|{_NUM_WORD})"
_NUM = rf"{_NUM_TOKEN}(?: (?:{_NUM_TOKEN}|and)){{0,3}}"

_LEAD_IN = (
    "ok|okay|alright|all right|right|so|and|now|then|well|please|just|quickly|kindly|yes|yeah|yep|also|actually|"
    "anyway|great|good|perfect|cool|thanks|thank you|"
    "lets|let us|let me|can we|can you|can i|could we|could you|could i|shall we|would you|will you|may we|may i|"
    "we can|we could|we should|we will|ill|i will|im|i am|were|we are|im going to|we are going to|were going to|"
    "im gonna|were gonna|gonna|going to|i want to|we want to|id like to|wed like to|i would like to|"
    "we would like to|i need to|we need to|i have to|we have to|time to|its time to|it is time to|if we could|"
    "if you could|if we can|if you can|if you would|if you dont mind|do you mind"
)
_LEADS = rf"(?:(?:{_LEAD_IN}) )*"

# Words of a lead-in that make even a bare "next slide" an instruction.
_REQUEST_WORDS = frozenset(
    "please lets let can could shall would will may kindly want like need time ill im going gonna".split()
)
# Discourse words trimmed from the front of the displayed phrase.
_DISCOURSE = frozenset(
    "ok okay alright all right so and now then well just quickly yes yeah yep also actually anyway great good "
    "perfect cool thanks thank".split()
)

_GOVERB = (
    r"(?:go|goes|going|move|moving|jump|jumping|skip|skipping|switch|switching|advance|advancing|continue|"
    r"continuing|proceed|flip|flipping|head|heading|get|return|returning|turn|turning|step|"
    r"take (?:us|me|everyone)|bring (?:us|me))"
)
_DIR = r"(?: on| ahead| forward| forwards| straight| right| back| backwards| backward| over| directly| all the way)?"
_GOV = rf"{_GOVERB}{_DIR}"
_TO = r"(?:to|onto|on to|over to|back to|up to|down to)"
_DET = r"(?:the |a |that |this |our |my |your )?"
_TARGET = (
    r"(?P<t>next (?:slide|1|one|page)|following slide|previous (?:slide|1|one)|prior slide|preceding slide|"
    r"last slide|final slide|closing slide|first slide|very first slide|opening slide|title slide|"
    r"beginning|start|end|"
    rf"slide (?:number |no )?(?P<n>{_NUM})|(?P<o>{_NUM}) slide)"
    r"(?P<of> of (?:the |this |our |my |your )?(?:deck|presentation|slides|slideshow|slide show|talk|show))?"
)
_REQUEST = (
    r"(?P<req>(?:can|could|may|shall|would|will|should) (?:we|you|i)|lets|let us|please|id like to|"
    r"i would like to|wed like to|we would like to|i want to|we want to)"
)

_SHOW = r"(?P<v>show(?: me| us| everyone)?|display|bring up|pull up|put up|open|open up)"
_REQ_VERB = r"(?:please |just |all |now )?(?P<v>see|have|get|look at|take a look at|view|do)"
_SLIDES = r"(?:slide|slides|page|pages)"
_RELDIR = r"(?P<dir>back|backwards|backward|forward|forwards|ahead)"
_PLEASE_T = (
    rf"(?P<t>next(?: slide| 1| one)?|previous slide|first slide|last slide|final slide|slide (?:number )?(?P<n>{_NUM}))"
)
_NOW_T = rf"(?P<t>next slide|previous slide|first slide|last slide|slide (?:number )?(?P<n>{_NUM}))"
_MISC_T = r"(?P<t>advance (?:the )?(?:slide|slides|deck)|skip (?:this|the current|that) (?:slide|one|1))"


def _rx(body: str) -> re.Pattern[str]:
    """Compile a grammar body anchored on token boundaries of the space-joined token string."""
    return re.compile(r"(?<![^ ])" + body + r"(?![^ ])")


# (kind, pattern). Kinds drive `_resolve`.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("to", _rx(rf"{_LEADS}(?P<v>{_GOV}|back|straight back|all the way back) {_TO} {_DET}(?:very )?{_TARGET}")),
    ("show", _rx(rf"{_LEADS}{_SHOW} {_DET}{_TARGET}")),
    ("request", _rx(rf"{_LEADS}{_REQUEST} {_REQ_VERB} {_DET}{_TARGET}")),
    ("please", _rx(rf"{_LEADS}{_PLEASE_T} please")),
    ("please", _rx(rf"{_LEADS}{_NOW_T} (?:now|thanks|thank you)")),
    ("bare", _rx(rf"{_LEADS}(?P<t>next slide|previous slide)")),
    ("rel", _rx(rf"{_LEADS}(?:(?P<v>{_GOVERB}) )?{_RELDIR}(?: up| by)? (?P<n>a|an|{_NUM}) {_SLIDES}")),
    ("rel", _rx(rf"{_LEADS}(?P<v>{_GOVERB}) (?P<n>a|an|{_NUM}) {_SLIDES} {_RELDIR}")),
    ("misc", _rx(rf"{_LEADS}{_MISC_T}")),
]

# The word right before a command phrase must not be one of these (it would attach the
# phrase to a noun phrase or a narrative instead of making it an instruction).
_ATTACHING = frozenset(
    """
    the a an this that these those my our your his her their its every each any some no another either neither
    which what whose
    on in of to from for at by with about into onto over under until till before after per during through via than
    as or very same whole entire
    i you we they he she it who id youd hed shed theyd youll theyll itll ive youve weve theyve youre theyre
    is are was were be been being am do does did has have had will would shall should can could may might must
    not dont doesnt didnt wont cant cannot couldnt shouldnt wouldnt isnt arent wasnt werent never
    say says said saying called named titled labeled labelled marked click clicking clicked press pressing pressed
    hit hitting tap tapping type typing phrase phrases words word command commands like
    went came come comes coming got gotten getting moved jumped skipped flipped returned refer refers referring
    back
    """.split()
)
_CLAUSE_BLOCKERS = frozenset(
    "before after when whenever if once until till unless while dont not never wont cant cannot couldnt shouldnt "
    "wouldnt didnt doesnt isnt without instead than rather whether".split()
)
_AS_SUBJECTS = frozenset("we you i they it soon".split())  # "as we go to ..." is narrative
# A bare "next slide" followed by one of these is a noun phrase, not an instruction.
_FOLLOW_BLOCK = frozenset(
    """
    deck decks show shows showed showing is was will would has had have does did can could should might may must
    isnt wasnt doesnt hasnt covers covered contains includes looks talks goes gets gives explains describes presents
    says said of in on for from that which where when there button transition number title already also
    """.split()
)


@dataclass(slots=True)
class _Candidate:
    start: int  # first token of the match (lead-ins included)
    end: int  # exclusive
    core: int  # first token after the absorbed lead-ins
    action: str  # next | prev | first | last | goto | rel
    n: int | None
    explicit: bool
    settle: bool  # needs right-hand context when it ends an interim
    extendable: bool  # ends in a number that more speech could extend


@dataclass(slots=True)
class _Pending:
    sig: tuple[Any, ...]
    since: int


class _Cursor:
    """A token position in the `carry + utterance` stream that survives interim revisions.

    The cursor remembers the utterance prefix it was set after; when a revision changes
    that prefix, the last few anchor words are searched nearby and the position follows them.
    If they were revised away, the position stays put (conservative: nothing re-triggers).
    """

    __slots__ = ("index", "_anchor")

    def __init__(self) -> None:
        self.index = 0
        self._anchor: list[str] | None = None

    def set(self, index: int, utt: Sequence[str], boundary: int) -> None:
        self.index = index
        self._anchor = list(utt[: index - boundary]) if index > boundary else None

    def realign(self, utt: Sequence[str], boundary: int) -> None:
        if self._anchor is None:
            return
        k = len(self._anchor)
        if list(utt[:k]) == self._anchor:
            return
        tail = self._anchor[-_ANCHOR_TAIL:]
        t = len(tail)
        best: int | None = None
        for p in range(max(t, k - _REALIGN_RADIUS), min(len(utt), k + _REALIGN_RADIUS) + 1):
            if list(utt[p - t : p]) == tail and (best is None or abs(p - k) < abs(best - k)):
                best = p
        if best is not None:
            self.index = boundary + best
            self._anchor = list(utt[:best])

    def after_final(self, shift: int, carry_len: int) -> None:
        self.index = min(max(0, self.index - shift), carry_len)
        self._anchor = None


class SlideTracker:
    def __init__(self, cues: list[SlideCue], config: TrackerConfig | None = None, current_index: int = 0) -> None:
        self.config = config or TrackerConfig()
        self._cues = sorted(cues, key=lambda c: c.index)
        self._pos = self._position_of(current_index)
        # Per-slide precomputation.
        self._kw: list[list[tuple[tuple[str, ...], str]]] = [self._prepare_keywords(c) for c in self._cues]
        self._script: list[list[str]] = [tm.tokenize(c.script).norms for c in self._cues]
        self._need: list[frozenset[str]] = [frozenset(tm.content_words(toks)) for toks in self._script]
        # Stream state.
        self._utt_norms: list[str] = []
        self._utt_len = 0
        self._carry_norms: list[str] = []
        self._carry_raws: list[str] = []
        self._carry_breaks: list[bool] = []
        self._floor = _Cursor()  # change point
        self._auto = _Cursor()  # auto floor (>= change point)
        self._pending: _Pending | None = None
        self._last_now: int | None = None
        self._last_change_ms: int | None = None
        self._last_command_ms: int | None = None
        self._dormant = False
        # Per-slide progress.
        self._covered: set[str] = set()
        self._need_cache: dict[str, str | None] = {}
        self._next_kws: list[tuple[int, tuple[str, ...], str, list[tuple[str, ...]]]] = []
        self._enter_slide()

    # -- public ---------------------------------------------------------------

    @property
    def current_index(self) -> int:
        """The tracker's belief about the current slide (a `SlideCue.index`; 0 for an empty deck)."""
        return self._cues[self._pos].index if self._cues else 0

    def set_current(self, index: int) -> None:
        """Sync with a slide change made elsewhere (manual navigation); resets per-slide progress."""
        pos = self._position_of(index)
        if pos == self._pos:
            return
        self._pos = pos
        n = len(self._carry_norms) + self._utt_len
        self._floor.set(n, self._utt_norms, len(self._carry_norms))
        self._auto.set(n, self._utt_norms, len(self._carry_norms))
        self._pending = None
        # The host took control: hold automatic changes for a cooldown, measured from the
        # latest result seen (set_current has no clock of its own).
        self._last_change_ms = self._last_now
        self._enter_slide()

    def feed(self, text: str, is_final: bool, now_ms: int | None = None) -> SlideDecision | None:
        """Feed one transcript result (interim or final).

        Returns a decision when the deck should move, and updates `current_index`
        to the decision's target. Returns None otherwise.
        """
        now = now_ms if now_ms is not None else int(time.monotonic() * 1000)
        self._last_now = now
        mode = self.config.mode
        if mode == "off":
            self._dormant = True
            return None
        tk = tm.tokenize(text)
        boundary = len(self._carry_norms)
        norms = self._carry_norms + tk.norms
        raws = self._carry_raws + tk.raws
        breaks = self._carry_breaks + tk.breaks
        self._utt_norms = tk.norms
        self._utt_len = len(tk.norms)
        self._floor.realign(tk.norms, boundary)
        self._auto.realign(tk.norms, boundary)
        if self._dormant:  # words spoken while auto-advance was off never count
            self._dormant = False
            self._floor.set(len(norms), tk.norms, boundary)
            self._auto.set(len(norms), tk.norms, boundary)

        decision: SlideDecision | None = None
        if len(self._cues) >= 2 and tk.norms:
            decision = self._scan_commands(norms, raws, breaks, boundary, is_final, now)
            if decision is None and mode in ("keywords", "script"):
                cfg = self.config
                if self._last_change_ms is not None and now - self._last_change_ms < cfg.cooldown_ms:
                    self._auto.set(len(norms), tk.norms, boundary)
                else:
                    decision = self._scan_keywords(norms, raws, boundary, is_final)
                    if decision is None and mode == "script":
                        decision = self._scan_script(norms, raws, boundary, is_final)
        if is_final:
            self._commit_final(norms, raws, breaks, boundary)
        return decision

    @property
    def script_coverage(self) -> float | None:
        """Share of the current slide's note content heard so far (None when the notes are empty)."""
        if not self._cues or not self._need[self._pos]:
            return None
        boundary = len(self._carry_norms)
        norms = self._carry_norms + self._utt_norms
        return self._coverage(norms, boundary, len(norms))

    def snapshot(self) -> dict[str, Any]:
        """Read-only debug view: belief, progress and pending candidates."""
        total = len(self._need[self._pos]) if self._cues else 0
        return {
            "current_index": self.current_index,
            "mode": self.config.mode,
            "slide_count": len(self._cues),
            "script_coverage": self.script_coverage,
            "script_words_total": total,
            "script_words_committed": len(self._covered),
            "pending": None if self._pending is None else " ".join(str(p) for p in self._pending.sig),
            "last_change_ms": self._last_change_ms,
            "last_command_ms": self._last_command_ms,
            "change_point": self._floor.index,
            "auto_floor": self._auto.index,
        }

    # -- setup ----------------------------------------------------------------

    def _position_of(self, index: int) -> int:
        if not self._cues:
            return 0
        for pos, cue in enumerate(self._cues):
            if cue.index == index:
                return pos
        return min(max(index, 0), len(self._cues) - 1)

    @staticmethod
    def _prepare_keywords(cue: SlideCue) -> list[tuple[tuple[str, ...], str]]:
        out: list[tuple[tuple[str, ...], str]] = []
        seen: set[tuple[str, ...]] = set()
        for kw in cue.keywords:
            toks = tuple(tm.tokenize(kw).norms)
            if not toks or toks in seen or all(t in tm.STOPWORDS for t in toks) or len("".join(toks)) < 2:
                continue
            seen.add(toks)
            out.append((toks, kw.strip()))
        return out

    def _enter_slide(self) -> None:
        """Reset per-slide progress and precompute the keyword targets reachable from here."""
        self._covered = set()
        self._need_cache = {}
        self._next_kws = []
        if not self._cues:
            return
        current = {toks for toks, _ in self._kw[self._pos]}
        last = len(self._cues) - 1
        reach = range(self._pos + 1, min(last if self.config.allow_keyword_skip else self._pos + 1, last) + 1)
        claimed: set[tuple[str, ...]] = set()
        for pos in reach:
            for toks, text in self._kw[pos]:
                if toks in current or toks in claimed:
                    continue
                claimed.add(toks)
                shadows = [c for c in current if len(c) > len(toks) and _contains(c, toks)]
                self._next_kws.append((pos, toks, text, shadows))

    # -- commands -------------------------------------------------------------

    def _scan_commands(
        self, norms: list[str], raws: list[str], breaks: list[bool], boundary: int, is_final: bool, now: int
    ) -> SlideDecision | None:
        floor = self._floor.index
        if floor >= len(norms):
            return None
        offs: list[int] = []
        c = 0
        for t in norms:
            offs.append(c)
            c += len(t) + 1
        text = " ".join(norms)
        cands: list[_Candidate] = []
        for kind, pat in _PATTERNS:
            for m in pat.finditer(text, offs[floor]):
                cand = self._resolve(kind, m, norms, offs)
                if cand is not None:
                    cands.append(cand)
        cands.sort(key=lambda x: (x.start, -x.end))
        blocked_until = -1
        for cand in cands:
            if cand.end <= blocked_until or cand.core < floor:
                continue
            verdict = self._judge(cand, norms, breaks, boundary, is_final, now)
            if verdict == "pending":
                return None
            if verdict == "reject":
                blocked_until = max(blocked_until, cand.end)
                continue
            target = self._target_pos(cand.action, cand.n)
            if target is None:  # no-op (already there, out of range): consume and move on
                blocked_until = max(blocked_until, cand.end)
                continue
            if self._last_command_ms is not None and now - self._last_command_ms < self.config.command_debounce_ms:
                # Echo of a command that already fired: drop it for good.
                self._consume(cand.end, boundary)
                blocked_until = max(blocked_until, cand.end)
                continue
            shown = cand.start
            while shown < cand.core and norms[shown] in _DISCOURSE:
                shown += 1
            matched = " ".join(raws[shown : cand.end])
            self._last_command_ms = now
            return self._apply(target, "command", matched, 1.0 if cand.explicit else 0.9, cand.end, boundary, now)
        return None

    def _resolve(self, kind: str, m: re.Match[str], norms: list[str], offs: list[int]) -> _Candidate | None:
        start = bisect_right(offs, m.start()) - 1
        end = bisect_right(offs, m.end())
        groups = m.groupdict()
        core_char = min(m.start(g) for g in ("v", "req", "t", "dir") if groups.get(g))
        core = bisect_right(offs, core_char) - 1
        explicit = True
        settle = False
        n: int | None = None
        extendable = False

        def number(group: str) -> tm.NumberMatch | None:
            at = bisect_right(offs, m.start(group)) - 1
            return tm.parse_number(norms, at)

        if kind in ("to", "show", "request"):
            t_start = bisect_right(offs, m.start("t")) - 1
            back = any(w in ("back", "backwards", "backward", "return", "returning") for w in norms[core:t_start])
            t = groups["t"]
            if groups.get("n"):
                num = number("n")
                if num is None or num.ordinal:
                    return None
                action, n = "goto", num.value
                end = bisect_right(offs, m.start("n")) - 1 + num.length
                extendable = _extendable(num.value, len(self._cues))
            elif groups.get("o"):
                num = number("o")
                if num is None or not num.ordinal:
                    return None
                action, n = "goto", num.value
            elif t.startswith(("next", "following")):
                action = "next"
            elif t.startswith(("previous", "prior", "preceding")):
                action = "prev"
            elif t == "last slide":
                action = "prev" if back else "last"
            elif t in ("final slide", "closing slide", "end slide"):
                action = "last"
            elif t.endswith("slide"):  # first / very first / opening / title slide
                action = "first"
            elif t in ("beginning", "start"):
                action = "first"
                settle = not groups.get("of")
            else:  # "end"
                action = "last"
                settle = not groups.get("of")
        elif kind == "please":
            t = groups["t"]
            if groups.get("n"):
                num = number("n")
                if num is None or num.ordinal:
                    return None
                action, n = "goto", num.value
            elif t.startswith("next"):
                action = "next"
            elif t.startswith("previous"):
                action = "prev"
            elif t.startswith("first"):
                action = "first"
            else:
                action = "last"
        elif kind == "bare":
            action = "next" if groups["t"].startswith("next") else "prev"
            requested = any(tok in _REQUEST_WORDS for tok in norms[start:core])
            explicit = requested
            settle = not requested
        elif kind == "rel":
            count = 1
            if groups["n"] not in ("a", "an"):
                num = number("n")
                if num is None or num.ordinal:
                    return None
                count = num.value
                if count == 0:
                    return None
            action = "rel"
            n = -count if groups["dir"].startswith("back") else count
        else:  # misc
            action = "next"
        return _Candidate(start, end, core, action, n, explicit, settle, extendable)

    def _judge(
        self, cand: _Candidate, norms: list[str], breaks: list[bool], boundary: int, is_final: bool, now: int
    ) -> str:
        """'fire', 'reject' or 'pending' for a grammatical command candidate."""
        s = cand.start
        fresh = s == 0 or breaks[s]
        if not fresh and norms[s - 1] in _ATTACHING:
            return "reject"
        if not fresh:
            j = s
            for _ in range(_CLAUSE_WINDOW):
                if j <= boundary or breaks[j]:
                    break
                j -= 1
                w = norms[j]
                if w in _CLAUSE_BLOCKERS or (w == "as" and j + 1 < len(norms) and norms[j + 1] in _AS_SUBJECTS):
                    return "reject"
        at_tail = cand.end >= len(norms)
        needs_context = cand.settle or (cand.extendable and at_tail)
        if not needs_context:
            return "fire"
        if not at_tail:
            follower = norms[cand.end]
            if cand.settle and not breaks[cand.end] and follower in _FOLLOW_BLOCK:
                return "reject"
            return "fire"
        if is_final:
            return "fire"
        sig = ("cmd", cand.action, cand.n, cand.start, " ".join(norms[cand.start : cand.end]))
        if self._pending is not None and self._pending.sig == sig:
            if now - self._pending.since >= self.config.command_settle_ms:
                return "fire"
        else:
            self._pending = _Pending(sig, now)
        return "pending"

    def _target_pos(self, action: str, n: int | None) -> int | None:
        cur, last = self._pos, len(self._cues) - 1
        if action == "next":
            target = cur + 1
        elif action == "prev":
            target = cur - 1
        elif action == "first":
            target = 0
        elif action == "last":
            target = last
        elif action == "goto":
            target = (n or 0) - 1
        else:  # rel
            target = min(max(cur + (n or 0), 0), last)
        if target < 0 or target > last or target == cur:
            return None
        return target

    # -- keywords -------------------------------------------------------------

    def _scan_keywords(self, norms: list[str], raws: list[str], boundary: int, is_final: bool) -> SlideDecision | None:
        if not self._next_kws:
            return None
        longest = max(len(toks) for _, toks, _, _ in self._next_kws) + 1
        lo = max(self._auto.index, boundary - longest, 0)
        for i in range(lo, len(norms)):
            first = norms[i][0]
            for pos, toks, text, shadows in self._next_kws:
                if toks[0][0] != first:
                    continue
                m = tm.match_phrase_at(norms, i, toks)
                if m is None or m.end <= boundary or m.start < self._auto.index:
                    continue
                if any(_covers(norms, m, shadow) for shadow in shadows):
                    continue
                if m.end >= len(norms) and not is_final:
                    sig = ("kw", pos, text, m.start)
                    if self._pending is None or self._pending.sig != sig:
                        self._pending = _Pending(sig, self._last_now or 0)
                        return None
                return self._apply(pos, "keyword", text, m.score, m.end, boundary, self._last_now or 0)
        return None

    # -- script ---------------------------------------------------------------

    def _scan_script(self, norms: list[str], raws: list[str], boundary: int, is_final: bool) -> SlideDecision | None:
        cur = self._pos
        if cur + 1 >= len(self._cues):
            return None
        need = self._need[cur]
        nxt = self._script[cur + 1]
        width = max(1, self.config.script_lookahead_words)
        if not need or len(nxt) < width:
            return None
        opening = nxt[: width + _OPENING_SLACK]
        lo = max(self._auto.index, boundary - width + 1, 0)
        for s in range(lo, len(norms) - width + 1):
            for o in range(len(opening) - width + 1):
                score = _run_score(norms, s, opening, o, width)
                if score == 0.0:
                    continue
                run = norms[s : s + width]
                if all(t in tm.STOPWORDS for t in run) or tm.find_phrase(self._script[cur], run) is not None:
                    continue
                coverage = self._coverage(norms, boundary, s)
                if coverage < self.config.script_coverage_threshold:
                    continue
                end = s + width
                if end >= len(norms) and not is_final:
                    sig = ("script", s, " ".join(run))
                    if self._pending is None or self._pending.sig != sig:
                        self._pending = _Pending(sig, self._last_now or 0)
                        return None
                confidence = score * (0.5 + 0.5 * min(coverage, 1.0))
                matched = " ".join(raws[s:end])
                return self._apply(cur + 1, "script", matched, confidence, end, boundary, self._last_now or 0)
        return None

    def _need_match(self, word: str) -> str | None:
        """The current notes' content stem that `word` (a spoken stem) stands for, if any."""
        need = self._need[self._pos]
        if word in need:
            return word
        cached = self._need_cache.get(word, "")
        if cached != "":
            return cached
        found = None
        if tm.max_edits(len(word)) > 0:
            for cand in need:
                if cand[0] == word[0] and tm.token_similarity(word, cand) > 0.0:
                    found = cand
                    break
        self._need_cache[word] = found
        return found

    def _heard(self, norms: Sequence[str], boundary: int, upto: int) -> set[str]:
        start = max(self._floor.index, boundary)
        heard: set[str] = set()
        for w in tm.content_words(norms[start:upto]):
            hit = self._need_match(w)
            if hit is not None:
                heard.add(hit)
        return heard

    def _coverage(self, norms: Sequence[str], boundary: int, upto: int) -> float:
        need = self._need[self._pos]
        if not need:
            return 0.0
        return len(self._covered | self._heard(norms, boundary, upto)) / len(need)

    # -- state changes --------------------------------------------------------

    def _apply(
        self, target: int, reason: Literal["command", "keyword", "script"], matched: str, confidence: float,
        end: int, boundary: int, now: int
    ) -> SlideDecision:
        self._pos = target
        self._consume(end, boundary)
        self._last_change_ms = now
        self._pending = None
        self._enter_slide()
        return SlideDecision(
            target_index=self._cues[target].index, reason=reason, matched=matched, confidence=round(confidence, 3)
        )

    def _consume(self, end: int, boundary: int) -> None:
        self._floor.set(end, self._utt_norms, boundary)
        self._auto.set(max(end, self._auto.index), self._utt_norms, boundary)

    def _commit_final(self, norms: list[str], raws: list[str], breaks: list[bool], boundary: int) -> None:
        if self.config.mode == "script" and self._cues:
            self._covered |= self._heard(norms, boundary, len(norms))
        keep = min(len(norms), _CARRY_TOKENS)
        shift = len(norms) - keep
        self._carry_norms = norms[shift:]
        self._carry_raws = raws[shift:]
        self._carry_breaks = breaks[shift:]
        self._floor.after_final(shift, keep)
        self._auto.after_final(shift, keep)
        self._pending = None
        self._utt_norms = []
        self._utt_len = 0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _extendable(value: int, deck_size: int) -> bool:
    """Could more speech extend this number to another valid slide ("twenty" -> "twenty one")?"""
    if 20 <= value <= 90 and value % 10 == 0:
        return deck_size > value
    return 1 <= value <= 9 and deck_size >= 100


def _contains(longer: Sequence[str], shorter: Sequence[str]) -> bool:
    n = len(shorter)
    return any(tuple(longer[i : i + n]) == tuple(shorter) for i in range(len(longer) - n + 1))


def _covers(norms: Sequence[str], m: tm.PhraseMatch, shadow: Sequence[str]) -> bool:
    """Did the speaker actually say the longer current-slide keyword `shadow` around match `m`?"""
    for start in range(max(0, m.start - len(shadow) + 1), m.start + 1):
        sm = tm.match_phrase_at(norms, start, shadow)
        if sm is not None and sm.start <= m.start and sm.end >= m.end:
            return True
    return False


def _run_score(norms: Sequence[str], s: int, opening: Sequence[str], o: int, width: int) -> float:
    total = 0.0
    for k in range(width):
        sim = tm.token_similarity(norms[s + k], opening[o + k])
        if sim == 0.0:
            return 0.0
        total += sim
    return total / width
