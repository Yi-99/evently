"""Text-matching primitives behind the slide tracker.

Pure functions, standard library only, cheap enough to run on every interim
STT result. Contracts the tracker (and its tests) rely on:

* `tokenize` turns raw STT text or authored notes/keywords into normalised
  tokens: lower-case ASCII letters and digits, accents folded, apostrophes
  removed ("let's" -> "lets"), hyphens/slashes split, "&" -> "and", filler
  noises ("um", "uh") dropped and single number words folded to digits
  ("five" -> "5", "twenty" -> "20"). It keeps each token's raw surface form
  for display and marks tokens preceded by an interior sentence break.
* `parse_number` reads a cardinal or ordinal at a token position: "5",
  "five", "fifth", "5th", "twenty one" (or its folded form "20 1"),
  "twenty first", "one hundred and five".
* `token_similarity`, `match_phrase_at` and `find_phrase` are the fuzzy
  matching primitives: exact tokens score 1.0; a plural-s difference 0.9;
  long words tolerate a bounded edit distance (never digits, never a
  different first letter); a phrase also matches when the spoken words are
  the same letters split or joined differently ("break down" == "breakdown").
* `stem` and `content_words` reduce notes and speech to comparable content
  stems for script coverage.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

FILLERS = frozenset(
    {"um", "uh", "uhm", "umm", "er", "erm", "ehm", "hmm", "hm", "mm", "mhm", "ah", "ahh", "eh", "uhh", "huh"}
)

# Function words and speaker tics that carry no slide-specific content.
STOPWORDS = frozenset(
    """
    a an the and or but so if then than because as of at by for from in into on onto to with without about over
    under up down out off through per via
    i me my mine we us our ours you your yours he him his she her hers it its they them their theirs who whom whose
    which what that this these those there here where when why how
    is am are was were be been being do does did doing have has had having will would shall should can could may
    might must not no nor
    dont doesnt didnt wont cant cannot isnt arent wasnt werent im ive ill id youre youve youll youd hes shes weve
    wed theyre theyve theyll theyd thats whats heres theres lets
    very really just also too only even still already yet again more most some any all each every both few much
    many such own same other another
    okay ok yeah yes right well like actually basically literally kind sort thing things lot lots bit little
    going gonna want wanted wants get got gets getting go goes went come comes came make makes made say says said
    see sees saw let look looks now
    """.split()
)

_SENTENCE_END = frozenset(".!?;:")
_TRAILING_PUNCT = "\"'’”)]}»"
_EDGE_PUNCT = ".!?;:,\"'’”“‘()[]{}«»…"
_WORD_RE = re.compile(r"[a-z0-9]+")
_SPLIT_RE = re.compile(r"[-_/\\+]")
_APOSTROPHE_RE = re.compile(r"[\'’‘ʼ`]")

_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}  # fmt: skip
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90,
}  # fmt: skip
_ORDINAL_UNITS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
    "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14,
    "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19,
}  # fmt: skip
_ORDINAL_TENS = {
    "twentieth": 20, "thirtieth": 30, "fortieth": 40, "fiftieth": 50, "sixtieth": 60, "seventieth": 70,
    "eightieth": 80, "ninetieth": 90,
}  # fmt: skip
_DIGITS_RE = re.compile(r"(\d{1,3})(st|nd|rd|th)?")

#: Every word `parse_number` understands (useful for building grammars).
NUMBER_WORDS = frozenset(_UNITS) | frozenset(_TENS) | frozenset(_ORDINAL_UNITS) | frozenset(_ORDINAL_TENS) | {"hundred"}


@dataclass(frozen=True, slots=True)
class Tokenized:
    """Normalised tokens plus, per token, its raw surface form and whether a sentence break precedes it."""

    norms: list[str]
    raws: list[str]
    breaks: list[bool]

    def __len__(self) -> int:
        return len(self.norms)

    def text(self, start: int, end: int) -> str:
        """Raw words `start..end` as the speaker said them (for UI display)."""
        return " ".join(self.raws[start:end])


def normalize(text: str) -> str:
    """Lower-case ASCII with apostrophes removed and separators turned into spaces (no tokenisation)."""
    if not text.isascii():
        text = "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))
    text = _APOSTROPHE_RE.sub("", text.lower())
    text = _SPLIT_RE.sub(" ", text).replace("&", " and ")
    return text


def fold_number_word(token: str) -> str:
    """"five" -> "5", "twenty" -> "20"; everything else unchanged (ordinals stay words)."""
    if token in _UNITS:
        return str(_UNITS[token])
    if token in _TENS:
        return str(_TENS[token])
    return token


def tokenize(text: str, *, drop_fillers: bool = True, fold_numbers: bool = True) -> Tokenized:
    """Split `text` into normalised tokens. See the module docstring for the normalisation rules.

    A sentence break is recorded on a token when the previous whitespace-separated
    chunk ended in `. ! ? ; :` (interior punctuation only: a terminator at the very end
    of the text has no following token and is therefore never recorded).
    """
    norms: list[str] = []
    raws: list[str] = []
    breaks: list[bool] = []
    pending_break = False
    for chunk in text.split():
        stripped = chunk.rstrip(_TRAILING_PUNCT)
        ends_sentence = bool(stripped) and stripped[-1] in _SENTENCE_END
        pieces = _WORD_RE.findall(normalize(chunk))
        if drop_fillers:
            pieces = [p for p in pieces if p not in FILLERS]
        for piece in pieces:
            raw = chunk.strip(_EDGE_PUNCT) if len(pieces) == 1 else piece
            norms.append(fold_number_word(piece) if fold_numbers else piece)
            raws.append(raw or piece)
            breaks.append(pending_break)
            pending_break = False
        pending_break = pending_break or ends_sentence
    return Tokenized(norms, raws, breaks)


def tokens(text: str) -> list[str]:
    """Convenience: the normalised tokens of `text`."""
    return tokenize(text).norms


@dataclass(frozen=True, slots=True)
class NumberMatch:
    value: int
    length: int  # tokens consumed
    ordinal: bool


def _below_hundred(toks: Sequence[str], i: int) -> tuple[int, int, bool] | None:
    """Parse a number < 100 at `toks[i]`: digits (optionally ordinal) or words, with tens+unit joins."""
    tok = toks[i]
    m = _DIGITS_RE.fullmatch(tok)
    if m:
        value, ordinal = int(m.group(1)), bool(m.group(2))
    elif tok in _UNITS:
        value, ordinal = _UNITS[tok], False
    elif tok in _TENS:
        value, ordinal = _TENS[tok], False
    elif tok in _ORDINAL_UNITS:
        return _ORDINAL_UNITS[tok], 1, True
    elif tok in _ORDINAL_TENS:
        return _ORDINAL_TENS[tok], 1, True
    else:
        return None
    if ordinal or value < 20 or value > 90 or value % 10 or i + 1 >= len(toks):
        return value, 1, ordinal
    nxt = toks[i + 1]  # "twenty one" / folded "20 1" / "twenty first"
    if nxt.isdigit() and 1 <= int(nxt) <= 9:
        return value + int(nxt), 2, False
    if nxt in _UNITS and 1 <= _UNITS[nxt] <= 9:
        return value + _UNITS[nxt], 2, False
    if nxt in _ORDINAL_UNITS and _ORDINAL_UNITS[nxt] <= 9:
        return value + _ORDINAL_UNITS[nxt], 2, True
    return value, 1, False


def parse_number(toks: Sequence[str], start: int = 0) -> NumberMatch | None:
    """Read a cardinal/ordinal (0..999) starting at `toks[start]`, or None if there is none."""
    n = len(toks)
    if start >= n:
        return None
    head = _below_hundred(toks, start)
    if head is None:
        return None
    value, length, ordinal = head
    i = start + length
    if not ordinal and 1 <= value <= 9 and length == 1 and i < n and toks[i] == "hundred":
        value *= 100
        i += 1
        j = i + 1 if i < n and toks[i] == "and" else i
        rest = _below_hundred(toks, j) if j < n else None
        if rest is not None:
            value += rest[0]
            i = j + rest[1]
            ordinal = rest[2]
        length = i - start
    return NumberMatch(value, length, ordinal)


def stem(token: str) -> str:
    """Light suffix stripping (plural, -ing, -ed) so paraphrased notes still line up."""
    if len(token) <= 3 or token.isdigit():
        return token
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith(("sses", "shes", "ches", "xes", "zes")):
        return token[:-2]
    base = token
    if token.endswith("ing") and len(token) >= 6:
        base = token[:-3]
    elif token.endswith("ed") and len(token) >= 6:
        base = token[:-2]
    elif token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    if base != token and len(base) >= 4 and base[-1] == base[-2] and base[-1] not in "ls":
        base = base[:-1]  # planning -> plan, stopped -> stop (but falling -> fall, passed -> pass)
    return base


def content_words(norms: Sequence[str]) -> list[str]:
    """Stems of the tokens that carry content (no stopwords, no one-letter tokens), in order."""
    return [stem(t) for t in norms if len(t) > 1 and t not in STOPWORDS]


def edit_distance(a: str, b: str, limit: int = 2) -> int:
    """Optimal string alignment distance (insert/delete/substitute/transpose), capped at `limit + 1`."""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if abs(la - lb) > limit:
        return limit + 1
    prev2: list[int] | None = None
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        row_min = i
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if prev2 is not None and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                v = min(v, prev2[j - 2] + 1)
            cur[j] = v
            if v < row_min:
                row_min = v
        if row_min > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[lb] if prev[lb] <= limit else limit + 1


def max_edits(length: int) -> int:
    """Edits tolerated for a word of `length` letters: none below 7, one below 12, two beyond."""
    if length < 7:
        return 0
    return 1 if length < 12 else 2


def token_similarity(spoken: str, target: str) -> float:
    """1.0 for an exact match, 0.9 for a plural-s difference, a bounded-edit score for long words, else 0.

    Digits never match fuzzily and the first letter must agree: STT errors keep the word
    onset far more often than they keep its length, and this rules out most short real-word
    neighbours ("price"/"pride" are too short anyway; "catering"/"watering" differ in onset).
    """
    if spoken == target:
        return 1.0
    if not spoken or not target or spoken[0] != target[0] or spoken.isdigit() or target.isdigit():
        return 0.0
    shorter = min(len(spoken), len(target))
    if shorter >= 3 and (spoken == target + "s" or target == spoken + "s"):
        return 0.9
    allowed = max_edits(shorter)
    if allowed == 0:
        return 0.0
    d = edit_distance(spoken, target, allowed)
    if d > allowed:
        return 0.0
    return 1.0 - d / max(len(spoken), len(target))


@dataclass(frozen=True, slots=True)
class PhraseMatch:
    start: int
    end: int  # exclusive token index
    score: float  # mean token similarity (0.95 for a split/joined compound)


def match_phrase_at(norms: Sequence[str], i: int, phrase: Sequence[str]) -> PhraseMatch | None:
    """Match `phrase` against `norms` starting exactly at token `i`.

    Tries a token-by-token fuzzy match first, then the same letters split or joined
    differently (one token more or fewer than the phrase has).
    """
    n = len(phrase)
    total_len = len(norms)
    if n == 0 or i >= total_len or not norms[i] or norms[i][0] != phrase[0][0]:
        return None
    if i + n <= total_len:
        total = 0.0
        for k in range(n):
            s = token_similarity(norms[i + k], phrase[k])
            if s == 0.0:
                break
            total += s
        else:
            return PhraseMatch(i, i + n, total / n)
    joined = "".join(phrase)
    for m in (n - 1, n + 1):
        if m >= 1 and i + m <= total_len and "".join(norms[i : i + m]) == joined:
            return PhraseMatch(i, i + m, 0.95)
    return None


def find_phrase(norms: Sequence[str], phrase: Sequence[str], start: int = 0, min_end: int = 0) -> PhraseMatch | None:
    """Earliest match of `phrase` in `norms` that starts at or after `start` and ends after `min_end`."""
    for i in range(max(start, 0), len(norms)):
        m = match_phrase_at(norms, i, phrase)
        if m is not None and m.end > min_end:
            return m
    return None
