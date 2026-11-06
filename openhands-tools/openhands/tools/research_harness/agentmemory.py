"""Authored memory notes and deterministic, bounded lexical retrieval.

The hashing features and reciprocal rank fusion adapt AgentMemory's
``agentmem/embeddings.py`` and ``agentmem/retrieve.py`` from
AhmadQasim881/All_Specs_Vulnerability_Harness_Design at commit
2d187316c038257effbe232d5cff7cc32c4e1fa7 (agentmemory/agentmem-research).
That package declares MIT in its pyproject.toml and README. Its research facade,
graph expansion, and execution workflows are deliberately not imported.

Hashed words and character trigrams are lexical features, not a semantic model.
Memory is authored context; an evidence reference does not verify a note.
"""

import hashlib
import json
import math
import re
from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, JsonValue, field_validator, model_validator

from .models import HarnessError, Record


MemoryKind = Literal["note", "decision", "constraint", "lesson"]
MemoryTag = Annotated[str, Field(min_length=1, max_length=64)]
MemoryReference = Annotated[str, Field(min_length=1, max_length=128)]
MEMORY_SEARCH_POLICY = "agentmemory-lexical/1"
MEMORY_RESULT_BYTES = 49_152
_COMMON_QUERY_WORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "this",
        "that",
        "what",
        "which",
        "where",
        "when",
        "why",
        "how",
        "should",
        "could",
        "would",
        "are",
        "was",
        "were",
        "has",
        "have",
        "had",
        "does",
        "did",
        "can",
        "will",
        "into",
        "onto",
        "your",
        "our",
        "next",
        "then",
        "please",
    }
)


def default_query_terms(query: str) -> list[str]:
    """Derive at most 32 literal terms for context's implicit question query."""
    terms = dict.fromkeys(
        word
        for word in re.findall(r"[^\W_]+", query.casefold())
        if len(word) >= 3 and word not in _COMMON_QUERY_WORDS
    )
    return list(terms)[:32]


def normalized_expiry(value: str | None) -> str | None:
    """Normalize an explicit, timezone-aware expiry for ordered SQLite storage."""
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise HarnessError("Memory expiry requires a timezone-aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError("Missing timezone")
        return parsed.astimezone(UTC).isoformat(timespec="microseconds")
    except (ValueError, OverflowError) as error:
        raise HarnessError(
            "Memory expiry requires a timezone-aware ISO timestamp"
        ) from error


def normalized_tags(tags: list[str]) -> list[str]:
    if not isinstance(tags, list) or len(tags) > 16:
        raise HarnessError("Memory requires at most 16 tags")
    result: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        if not isinstance(tag, str) or not 1 <= len(tag) <= 64 or not tag.strip():
            raise HarnessError("Memory tags require 1..64 nonblank characters")
        value = tag.strip()
        if value.casefold() not in seen:
            result.append(value)
            seen.add(value.casefold())
    return result


def tag_token(tag: str) -> str:
    """JSON escaping makes newline-delimited tokens unambiguous for any tag."""
    return "\n" + json.dumps(tag.casefold(), ensure_ascii=False) + "\n"


class MemoryRecord(Record):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    id: MemoryReference
    thread_id: MemoryReference
    text: str = Field(min_length=1, max_length=8192)
    source: str = Field(min_length=1, max_length=2048)
    kind: MemoryKind = "note"
    tags: list[MemoryTag] = Field(default_factory=list, max_length=16)
    evidence_ids: list[MemoryReference] = Field(default_factory=list, max_length=32)
    importance: int = Field(default=5, ge=0, le=10)
    pinned: bool = False
    expires_at: str | None = Field(default=None, max_length=64)
    status: Literal["active", "forgotten"] = "active"
    forget_reason: str | None = Field(default=None, max_length=4096)
    version: int = Field(ge=1)
    created_at: str
    updated_at: str

    @field_validator("expires_at")
    @classmethod
    def validate_expiry(cls, value: str | None) -> str | None:
        return normalized_expiry(value)

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, values: list[str]) -> list[str]:
        return normalized_tags(values)

    @field_validator("evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise HarnessError("Memory evidence references must not be blank")
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def validate_memory(self) -> Self:
        if not self.text.strip() or not self.source.strip():
            raise HarnessError("Memory text and source must not be blank")
        if self.status == "forgotten":
            if not self.forget_reason or not self.forget_reason.strip():
                raise HarnessError("Forgetting memory requires a reason")
        elif self.forget_reason is not None:
            raise HarnessError("Active memory cannot have a forget reason")
        return self


def search_text(record: MemoryRecord) -> str:
    return "\n".join([record.text, *record.tags]).casefold()


def hashing_features(text: str) -> list[float]:
    """AgentMemory word/trigram hashing, with fixed dimensions and no model I/O."""
    vector = [0.0] * 256
    for word in text.lower().split():
        features = (word, *(word[index : index + 3] for index in range(len(word) - 2)))
        for feature in features:
            digest = hashlib.md5(feature.encode("utf-8"), usedforsecurity=False)
            hashed = int(digest.hexdigest(), 16)
            vector[hashed % 256] += 1.0 if (hashed >> 8) & 1 else -1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def reciprocal_rank_fusion(ranked_lists: list[list[str]]) -> dict[str, float]:
    """Fuse rankings using AgentMemory's reciprocal-rank constant of 60."""
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, identifier in enumerate(dict.fromkeys(ranked)):
            scores[identifier] = scores.get(identifier, 0.0) + 1.0 / (60 + rank + 1)
    return scores


def rank_memory(
    records: list[MemoryRecord],
    query: str,
    *,
    query_terms: list[str] | None = None,
) -> list[dict[str, JsonValue]]:
    """Rank only prefiltered candidates; never introduce an unrelated candidate."""
    features = hashing_features(query)
    similarities = {
        record.id: sum(
            left * right
            for left, right in zip(features, hashing_features(search_text(record)))
        )
        for record in records
    }
    rankings = [
        [
            record.id
            for record in sorted(
                records, key=lambda record: (-similarities[record.id], record.id)
            )
        ],
        [
            record.id
            for record in sorted(
                records, key=lambda record: (-record.importance, record.id)
            )
        ],
        [
            record.id
            for record in sorted(
                records, key=lambda record: (record.updated_at, record.id), reverse=True
            )
        ],
    ]
    scores = reciprocal_rank_fusion(rankings)
    ordered = sorted(
        records,
        key=lambda record: (not record.pinned, -scores[record.id], record.id),
    )
    normalized_query = query.casefold().strip()

    def matches(record: MemoryRecord) -> bool:
        text = search_text(record)
        return (
            normalized_query in text
            if query_terms is None
            else any(term in text for term in query_terms)
        )

    return [
        {
            "record": record.model_dump(mode="json"),
            "score": scores[record.id],
            "lexical_similarity": similarities[record.id],
            "matched_query": matches(record),
            "selection_reason": (
                "default_term_match"
                if query_terms is not None and matches(record)
                else "literal_match"
                if normalized_query and matches(record)
                else "browse"
                if not normalized_query and query_terms is None
                else "pinned_constraint"
            ),
        }
        for record in ordered
    ]
