from datetime import date

from pydantic import AliasChoices, BaseModel, Field

# Archive categorization caps (editorial metadata, not used by duplication).
CATEGORY_MAX = 80
TAG_MAX = 60
MAX_TAGS = 20


class CheckRequest(BaseModel):
    """Body for POST /check. React frontend sends `text`; the legacy HTML
    page sends `path` — accept either."""

    text: str = Field(
        min_length=2,
        max_length=2000,
        validation_alias=AliasChoices("text", "path"),
    )

    model_config = {"populate_by_name": True}


class AddRequest(BaseModel):
    """Body for POST /add — a new archive entry."""

    text: str = Field(
        min_length=2,
        max_length=2000,
        validation_alias=AliasChoices("text", "path"),
    )
    # year is derived from message_date by the DB (generated column).
    message_date: date | None = None
    # Editorial metadata: one broad category + free tags.
    category: str | None = Field(default=None, max_length=CATEGORY_MAX)
    tags: list[str] = Field(default_factory=list, max_length=MAX_TAGS)

    model_config = {"populate_by_name": True}


class UpdateRequest(BaseModel):
    """Body for PATCH /messages/{id} — partial update of one message.

    Only fields present in the request body are changed (tracked via
    model_fields_set). Changing `text` triggers re-embedding.
    """

    text: str | None = Field(default=None, min_length=2, max_length=2000)
    message_date: date | None = None
    # Empty string clears the category (stored as NULL).
    category: str | None = Field(default=None, max_length=CATEGORY_MAX)
    tags: list[str] | None = Field(default=None, max_length=MAX_TAGS)


class ImportRow(BaseModel):
    """One row of a bulk import (CSV or pasted)."""

    message: str = Field(min_length=2, max_length=2000)
    # JSON key is `date`; named message_date here to avoid shadowing the type.
    message_date: date | None = Field(
        default=None, validation_alias=AliasChoices("date", "message_date")
    )
    category: str | None = Field(default=None, max_length=CATEGORY_MAX)
    tags: list[str] = Field(default_factory=list, max_length=MAX_TAGS)

    model_config = {"populate_by_name": True}


class ImportRequest(BaseModel):
    """Body for POST /import — rows are parsed client-side (CSV or paste)."""

    rows: list[ImportRow] = Field(min_length=1, max_length=5000)


class CategoryRequest(BaseModel):
    """Body for POST /categories."""

    name: str = Field(min_length=1, max_length=CATEGORY_MAX)
