import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

Keyword = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class QueryExpansion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    original_query: str = Field(min_length=1, max_length=300)
    translated_query: str = Field(min_length=1, max_length=300)
    primary_keywords: list[Keyword] = Field(min_length=1, max_length=5)
    related_keywords: list[Keyword] = Field(max_length=8)
    hashtags: list[Keyword] = Field(max_length=8)
    negative_keywords: list[Keyword] = Field(max_length=8)
    topics: list[Keyword] = Field(max_length=8)

    @field_validator("translated_query")
    @classmethod
    def chinese_translation(cls, value: str) -> str:
        if not re.search(r"[\u4e00-\u9fff]", value):
            raise ValueError("Translation must contain Chinese text.")
        return value.strip()

    @field_validator(
        "primary_keywords", "related_keywords", "hashtags", "negative_keywords", "topics"
    )
    @classmethod
    def normalize_terms(cls, terms: list[str]) -> list[str]:
        values = [term.lstrip("#＃").strip() for term in terms]
        if any(not term for term in values):
            raise ValueError("Empty keyword.")
        return list(dict.fromkeys(values))

    def queries(self, budget: int = 10) -> list[str]:
        candidates = [
            self.translated_query,
            *self.primary_keywords,
            *self.related_keywords,
            *self.hashtags,
        ]
        return list(dict.fromkeys(candidates))[:budget]


class QueryPlan(BaseModel):
    subject: str = ""
    action: str | None = None
    mandatory_attributes: list[str] = Field(default_factory=list)
    modifiers: list[str] = Field(default_factory=list)
    tiered_queries: list[str] = Field(default_factory=list)  # [primary, natural, narrow]


class ExpansionOutcome(BaseModel):
    original_query: str
    source: Literal["gemini", "fallback", "manual", "original", "dictionary"]
    expansion: QueryExpansion | None = None
    plan: QueryPlan | None = None
    queries: list[str]
    warning: str | None = None
    error_code: str | None = None
    cached: bool = False


class ExpandRequest(BaseModel):
    query: str = Field(min_length=1, max_length=300)
    use_ai: bool = True

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Please enter a query.")
        return value
