"""Word → sentence joiner.

Groups the flat word stream of a whole episode into sentence-level lines for
the classifier, and derives a silence map from the gaps between words. Pure
and deterministic; ``JOINER_VERSION`` is stored with every transcript so
stored words can be re-joined when these rules change.

The stream is joined across ASR segments: in the batched pipeline a segment
is a VAD chunk, and 14% of chunk boundaries fall mid-sentence. Segment
indices survive only as provenance (``Sentence.asr_segments``).

Times are compared in integer centiseconds and probabilities in whole
percent, the resolution faster-whisper emits and the word codec stores.
Differences of 2-dp floats are noisy (12.34 - 11.74 = 0.5999999999999996):
170 of the 96,782 gaps in the 10-episode corpus sit exactly on the 0.60 s
threshold, and float subtraction puts 89 of them below it. Working at storage
resolution also makes a re-join of decoded words exact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from .codec import centiseconds, percent
from .protocol import AsrSegmentMeta, Sentence, SilenceRegion, Word

JOINER_VERSION = 1


@dataclass(frozen=True)
class JoinerParams:
    """Defaults fit the pause distributions of 10 h of tiny.en: after a word
    that ends a sentence by punctuation the pause has mean 1.03 s, median
    0.62 s, and p25 0.34 s; after any other word, p95 0.12 s and p99 0.54 s."""

    gap_hard: float = 0.60  # a pause this long ends a sentence without punctuation
    gap_override: float = 0.60  # a pause this long after "." beats every guard
    gap_soft: float = 0.25  # the least pause a cap break prefers to cut at
    max_duration_s: float = 18.0
    max_chars: int = 320
    min_duration_s: float = 0.30  # shorter punct-ended lines ("Uh.") glue forward
    merge_window_s: float = 0.05  # shorter pauses are timestamp jitter; they count as none
    low_conf_mean_p: float = 0.55
    # Not 0.30: 1.15% of words fall below 0.30, so with ~11 words a line, one
    # stray word flagged 10.7% of all sentences (independent per-word noise
    # predicts 11.6%). Below 0.10 (0.11% of words) the flag marks 2.1% of
    # lines and still catches every "thisamericanlife.org slash life" promo.
    low_conf_min_p: float = 0.10
    repeat_unigram_run: int = 6
    repeat_ngram_run: int = 4
    cr_flag: float = 2.4
    silence_min_s: float = 3.0


TERMINALS = frozenset(".!?。！？")
SOFT_PUNCTUATION = frozenset(",;:—、，")
# Stripped before looking for the terminal: ' "Stop."' and ' (done.)' end sentences.
CLOSERS = "\"'”’)]}»"
# faster-whisper's prepend set, plus the curly single quote.
OPENERS = "\"'“‘¿([{-"
DISCOURSE_MARKERS = frozenset(
    {"and", "but", "so", "then", "because", "which", "when", "cause", "'cause", "now", "anyway", "ok", "okay"}
)

# "." guards. Each only *suppresses* a boundary, and only below gap_override.
# Counts are hits in the 10-episode, 10 h tiny.en corpus.
#
# Abbreviations that precede a name never end a sentence, so they suppress
# unconditionally: Dr. 32, Mr. 10, Ms. 10, St. 3, Mrs. 1, all below the
# override gap, and 54 of the 56 are real titles ("Dr. Fuchs"; the misses are
# "call you Mr." and "Miss or Mrs.").
TITLE_ABBREVIATIONS = frozenset(
    "mr mrs ms mx dr prof rev fr st mt ft gen col sgt lt capt sen rep gov pres vs".split()
)
# Abbreviations that are also ordinary sentence-final words suppress only
# before a lowercase or numeric continuation ("No. 5", "6 p.m. at night"). All
# 16 "no."/"am." hits below the override gap end a sentence ("I said no. I
# did", "I am. Rand"), as do 5 of the 7 "p.m." ("11 p.m. She opened").
FINAL_ABBREVIATIONS = frozenset(
    "sr jr ave blvd rd inc corp ltd co llc etc approx dept est fig no vol al eg ie am pm".split()
)
# A single-letter initial ("John C. Stennis", "Roe v. Wade"), except the
# pronoun: "neither did I." ends its sentence.
_INITIAL = re.compile(r"[A-HJ-Za-z]")
_INITIALISM = re.compile(r"(?:[A-Za-z]\.)+[A-Za-z]")  # U.S., J.O., W.B.E.S.
_DOMAIN = re.compile(r"\.(?:com|org|net|io|co|gov|edu|us|fm|tv|ly)$", re.IGNORECASE)

# A cap break only looks for a natural cut in the trailing 40% of the buffer,
# so the emitted line keeps most of it and the carried remainder stays short.
_CAP_KEEP_FRACTION = 0.6
# faster-whisper splits "W.B.E.S." into four words and "800-273-TALK." into
# three; the guards need the whole token. Bounded so unspaced (CJK) text,
# where no word starts with a space, stays linear.
_MAX_TOKEN_PIECES = 8


@dataclass(frozen=True, slots=True)
class _Word:
    """A non-blank input word at storage resolution, clamped to be monotone."""

    index: int  # position in the caller's list
    start: int  # centiseconds
    end: int
    text: str
    pct: int
    segment: int


def _prepare(words: Sequence[Word]) -> list[_Word]:
    stream: list[_Word] = []
    floor = 0
    for index, word in enumerate(words):
        if not word.word.strip():
            continue
        # At VAD splices a word can start (rarely, even end) before the
        # previous one ends, because faster-whisper resolves each word to the
        # chunk containing its midpoint. Clamping keeps sentences and silences
        # from overlapping.
        start = max(centiseconds(word.start), floor)
        end = max(centiseconds(word.end), start)
        stream.append(_Word(index, start, end, word.word, percent(word.probability), word.segment))
        floor = end
    return stream


def _text(words: Sequence[_Word]) -> str:
    # Words carry their own leading space and merged punctuation, so plain
    # concatenation is exact for English and for unspaced (CJK) text alike.
    return "".join(w.text for w in words).strip()


def _core(text: str) -> str:
    return text.strip().rstrip(CLOSERS)


def _gap(before: _Word, after: _Word, params: JoinerParams) -> float:
    gap = (after.start - before.end) / 100
    return gap if gap >= params.merge_window_s else 0.0


def _token(stream: Sequence[_Word], pos: int) -> str:
    """The whitespace-delimited token ending at ``stream[pos]``: " U" + ".S." is "U.S."."""
    first = pos
    while first > 0 and pos - first < _MAX_TOKEN_PIECES - 1 and not stream[first].text[:1].isspace():
        first -= 1
    return _text(stream[first : pos + 1])


def is_sentence_end(
    token: str, next_word: str | None, gap: float, params: JoinerParams = JoinerParams()
) -> bool:
    """Whether ``token`` ends a sentence, given the raw next word (leading space
    included) and the pause before it in seconds.

    Only "." has guards: "!", "?" and the CJK terminals always end a sentence,
    and so does any terminal followed by a pause of at least ``gap_override``
    (a 0.9 s pause after "Dr." is still a boundary). Numbered-list markers
    ("1995." before a capital) deliberately get no guard: about half are real
    sentence ends, and a spurious split costs the classifier nothing.
    """
    core = _core(token)
    if not core or core[-1] not in TERMINALS:
        return False
    if core[-1] != "." or gap >= params.gap_override:
        return True
    return not _period_guarded(core, next_word or "")


def _period_guarded(core: str, next_word: str) -> bool:
    """True when the final "." of ``core`` does not end the sentence."""
    if core.endswith("..."):  # G1, 70 hits: trailing off, usually mid-utterance
        return True
    if core[-2:-1].isdigit() and next_word[:1].isdigit():  # G2: "3." + "5"
        return True
    following = next_word.strip().lstrip(OPENERS)[:1]
    body = core[:-1].lstrip(OPENERS)
    key = body.replace(".", "").lower()
    if key in FINAL_ABBREVIATIONS:
        return following.islower() or following.isdigit()
    return bool(
        _INITIAL.fullmatch(body)  # G3, 15 hits
        or _INITIALISM.fullmatch(body)  # G4, 10 hits
        or key in TITLE_ABBREVIATIONS  # G5
        # G6, 8 hits: "thisamericanlife.org. around 11" continues; ".org. Graham" does not.
        or (_DOMAIN.search(body) and following.islower())
    )


def _is_discourse_marker(text: str) -> bool:
    normalised = "".join(ch for ch in text.lower().replace("’", "'") if ch.isalnum() or ch == "'")
    return normalised in DISCOURSE_MARKERS


def _cap_break(stream: Sequence[_Word], first: int, last: int, params: JoinerParams) -> int:
    """Position of the last word to emit when ``stream[first..last]`` hit a length cap.

    Prefers, in order: the widest pause of at least ``gap_soft``, the last
    soft punctuation, the word before the last discourse marker, and finally
    the word that crossed the cap. The caller guarantees ``stream[last + 1]``
    exists, so every candidate has a following word.
    """
    lo = min(last, first + int((last - first + 1) * _CAP_KEEP_FRACTION))
    cuts = range(lo, last + 1)
    gap, widest = max((_gap(stream[k], stream[k + 1], params), k) for k in cuts)
    if gap >= params.gap_soft:
        return widest
    for k in reversed(cuts):
        if _core(stream[k].text)[-1:] in SOFT_PUNCTUATION:
            return k
    for k in reversed(cuts):
        if _is_discourse_marker(stream[k + 1].text):
            return k
    return last


def repeat_runs(tokens: Sequence[str], n: int, min_repeats: int) -> list[tuple[int, int]]:
    """Maximal spans ``[lo, hi)`` of ``tokens`` made of at least ``min_repeats``
    back-to-back copies of one n-gram (a trailing partial copy is included)."""
    spans: list[tuple[int, int]] = []
    i = 0
    while i + n < len(tokens):
        j = i
        while j + n < len(tokens) and tokens[j] == tokens[j + n]:
            j += 1
        # tokens[i : j + n] now has period n.
        if (j + n - i) // n >= min_repeats:
            spans.append((i, j + n))
        i = max(j, i + 1)
    return spans


def _repeated_positions(stream: Sequence[_Word], params: JoinerParams) -> set[int]:
    """Stream positions inside a repetition run.

    faster-whisper's own loop guards (compression_ratio_threshold and friends)
    are read only by its sequential decoder, so under the batched pipeline this
    is the only detector. Runs are found over the whole stream rather than per
    sentence, so a loop that punctuation cuts into short lines ("Thank you.
    Thank you. ...") is still caught.
    """
    normalised = [(pos, "".join(ch for ch in w.text.lower() if ch.isalnum())) for pos, w in enumerate(stream)]
    normalised = [(pos, token) for pos, token in normalised if token]
    tokens = [token for _, token in normalised]
    marked: set[int] = set()
    for n in (1, 2, 3, 4):
        need = params.repeat_unigram_run if n == 1 else params.repeat_ngram_run
        for lo, hi in repeat_runs(tokens, n, need):
            marked.update(pos for pos, _ in normalised[lo:hi])
    return marked


def _sentence(
    index: int,
    stream: Sequence[_Word],
    first: int,
    last: int,
    reason: str,
    repeated: set[int],
    compression: dict[int, float],
    params: JoinerParams,
) -> Sentence:
    words = stream[first : last + 1]
    pcts = [w.pct for w in words]
    min_p = min(pcts) / 100
    mean_p = sum(pcts) / (100 * len(pcts))
    segments = tuple(dict.fromkeys(w.segment for w in words))
    start, end = words[0].start, words[-1].end
    flags = []
    # Flags never drop a line: the least confident audio is compressed
    # voice-over under music, which is to say ads.
    if mean_p < params.low_conf_mean_p or min_p < params.low_conf_min_p:
        flags.append("low_confidence")
    if not repeated.isdisjoint(range(first, last + 1)):
        flags.append("repetitive")
    if any(compression.get(segment, 0.0) > params.cr_flag for segment in segments):
        flags.append("high_compression")
    if (end - start) / 100 > params.max_duration_s:
        flags.append("long_span")
    return Sentence(
        index=index,
        # No padding correction: word ends already sit ~0.32 s inside the VAD
        # chunk ends (exactly speech_pad_ms), so trimming would clip speech.
        start=start / 100,
        end=end / 100,
        text=_text(words),
        word_start=words[0].index,
        word_count=words[-1].index - words[0].index + 1,
        break_reason=reason,
        soft_end=reason == "cap",
        min_p=min_p,
        mean_p=round(mean_p, 4),
        asr_segments=segments,
        flags=tuple(flags),
    )


def join_words(
    words: Sequence[Word],
    segments: Sequence[AsrSegmentMeta] = (),
    params: JoinerParams = JoinerParams(),
) -> list[Sentence]:
    """Group a flat, time-ordered word stream into sentences. Pure.

    Blank words are skipped (so blank or empty input yields no sentences):
    they belong to no sentence, and a sentence's ``word_start``/``word_count``
    span into ``words`` may include blanks between its first and last word.
    Every other word lands in exactly one sentence, in order, and sentences
    never overlap.
    """
    stream = _prepare(words)
    repeated = _repeated_positions(stream, params)
    compression = {segment.index: segment.compression_ratio for segment in segments}
    sentences: list[Sentence] = []

    def emit(last: int, reason: str) -> None:
        sentences.append(_sentence(len(sentences), stream, first, last, reason, repeated, compression, params))

    first = 0
    for pos, word in enumerate(stream):
        if pos + 1 == len(stream):
            emit(pos, "eof")
            break
        following = stream[pos + 1]
        gap = _gap(word, following, params)
        duration = (word.end - stream[first].start) / 100
        # A too-short line ("Uh.") glues forward only when speech follows
        # closely; before a pause long enough to break anyway, the
        # punctuation is the better label.
        if (duration >= params.min_duration_s or gap >= params.gap_hard) and is_sentence_end(
            _token(stream, pos), following.text, gap, params
        ):
            emit(pos, "punct")
        elif gap >= params.gap_hard:
            emit(pos, "gap")
        elif duration >= params.max_duration_s or len(_text(stream[first : pos + 1])) >= params.max_chars:
            cut = _cap_break(stream, first, pos, params)
            emit(cut, "cap")
            first = cut + 1  # the words after the cut carry into the next sentence
            continue
        else:
            continue
        first = pos + 1
    return sentences


def derive_silences(
    words: Sequence[Word], duration: float | None, params: JoinerParams = JoinerParams()
) -> list[SilenceRegion]:
    """Stretches without words: the head before the first word and every
    inter-word gap, each if at least ``silence_min_s``, and the tail from the
    last word to ``duration`` whenever the audio runs past it, however briefly
    — trailing music has no words, and the classifier must see where the
    audio really ends. Pure.

    With no words, a known positive duration is one "tail" from 0 (the tail
    after a last word that never came); with no duration either, nothing.
    Regions never overlap ``join_words`` sentences while ``gap_hard <=
    silence_min_s``, because every such gap is then a sentence break.

    Word gaps beat a VAD pass as a silence map because VAD scores music beds
    as speech: on episode 01 they yield 50 regions (475 s) where VAD finds 38
    (285 s), 37 of them inside a word gap.
    """
    stream = _prepare(words)
    min_gap = params.silence_min_s
    regions: list[SilenceRegion] = []
    if stream and stream[0].start / 100 >= min_gap:
        regions.append(SilenceRegion(0.0, stream[0].start / 100, "head"))
    for before, after in zip(stream, stream[1:]):
        if (after.start - before.end) / 100 >= min_gap:
            regions.append(SilenceRegion(before.end / 100, after.start / 100, "gap"))
    last_end = stream[-1].end if stream else 0
    if duration is not None and centiseconds(duration) > last_end:
        regions.append(SilenceRegion(last_end / 100, centiseconds(duration) / 100, "tail"))
    return regions
