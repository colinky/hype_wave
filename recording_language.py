"""Compare recording-edition labels and localized titles without inferring the sung language."""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

_EDITIONS = {
    "english": ("eng", "english", "영어"),
    "korean": ("kr", "kor", "korean", "한국어"),
    "japanese": ("jp", "jpn", "japanese", "일본어"),
    "chinese": ("cn", "chn", "chinese", "mandarin", "중국어"),
}
_LANGUAGE = {alias: edition for edition, aliases in _EDITIONS.items() for alias in aliases}
_LABEL = re.compile(
    r"\b(eng|english|kr|kor|korean|jp|jpn|japanese|cn|chn|chinese|mandarin)[ .-]*(?:version|ver|edition)\b\.?"
    r"|(영어|한국어|일본어|중국어)\s*(?:버전|판)", re.I | re.ASCII,
)
_SCRIPTS = {"hangul": re.compile(r"[가-힣]"), "kana": re.compile(r"[ぁ-ヿ]"), "han": re.compile(r"[一-鿿]")}


def normalize_edition_labels(text: str) -> str:
    return _LABEL.sub(lambda m: _LANGUAGE[(m[1] or m[2]).lower()] + " version", text)


@dataclass(frozen=True)
class RecordingLanguage:
    artists: frozenset[str]
    latin: frozenset[str]
    local: frozenset[str]
    scripts: frozenset[str]
    editions: frozenset[str]

    @classmethod
    def from_metadata(cls, titles: Iterable[str | None], artists: Iterable[str | None]) -> RecordingLanguage:
        normalized = [unicodedata.normalize("NFKC", t).casefold() for t in titles if t]
        editions = {_LANGUAGE[(m[1] or m[2])] for t in normalized for m in _LABEL.finditer(t)}
        stems = [_LABEL.sub("", t) for t in normalized]
        latin = {" ".join(re.findall(r"[a-z0-9]+", t)) for t in stems}
        local = {"".join(re.findall(r"[가-힣ぁ-ヿ一-鿿]", t)) for t in stems}
        scripts = {script for script, pattern in _SCRIPTS.items() if any(pattern.search(t) for t in local)}
        credits = {" ".join(unicodedata.normalize("NFKC", a).casefold().split()) for a in artists if a}
        return cls(frozenset(credits), frozenset(latin - {""}), frozenset(local - {""}),
                   frozenset(scripts), frozenset(editions))

    def shares_title(self, other: RecordingLanguage) -> bool:
        return bool(self.latin & other.latin or self.local & other.local)

    def different_scripts(self, other: RecordingLanguage) -> bool:
        return bool(self.scripts and other.scripts and self.scripts.isdisjoint(other.scripts))

    def conflicts_with(self, candidate: RecordingLanguage) -> bool:
        if not self.artists & candidate.artists:
            return False
        if candidate.editions and self.editions != candidate.editions and self.shares_title(candidate):
            return True
        return self.different_scripts(candidate) and (not self.latin or bool(self.latin & candidate.latin))

    def supports(self, candidate: RecordingLanguage) -> bool:
        if not self.artists & candidate.artists or self.editions != candidate.editions:
            return False
        return bool(self.editions or any(t in c for t in self.local for c in candidate.local))
