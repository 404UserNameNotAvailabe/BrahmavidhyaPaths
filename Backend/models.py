from datetime import date
from typing import Annotated

from pydantic import AfterValidator, AliasChoices, BaseModel, Field

import taxonomy

# Archive metadata caps (editorial, not used by duplication).
TAG_MAX = 60
MAX_TAGS = 20
SOURCE_MAX = 120

# A Dhanurmas date (16 Dec – 14 Jan) or None.
SeasonDate = Annotated[date | None, AfterValidator(taxonomy.check_season_date)]
# Original-source URL; blank clears it (→ None).
Link = Annotated[
    str | None,
    Field(max_length=taxonomy.LINK_MAX),
    AfterValidator(taxonomy.clean_link),
]


class CheckRequest(BaseModel):
    """Body for POST /check."""

    text: str = Field(min_length=2, max_length=2000)


class AddRequest(BaseModel):
    """Body for POST /add — a new archive entry."""

    text: str = Field(min_length=2, max_length=2000)
    # year + season are derived from message_date by the DB (generated columns).
    message_date: SeasonDate = None
    tags: list[str] = Field(default_factory=list, max_length=MAX_TAGS)
    source: str | None = Field(default=None, max_length=SOURCE_MAX)
    link: Link = None


class UpdateRequest(BaseModel):
    """Body for PATCH /messages/{id} — partial update of one message.

    Only fields present in the request body are changed (tracked via
    model_fields_set). Changing `text` triggers re-embedding. An empty `link`
    clears it.
    """

    text: str | None = Field(default=None, min_length=2, max_length=2000)
    message_date: SeasonDate = None
    tags: list[str] | None = Field(default=None, max_length=MAX_TAGS)
    source: str | None = Field(default=None, max_length=SOURCE_MAX)
    link: Link = None
    is_favorite: bool | None = None


class ImportRow(BaseModel):
    """One row of a bulk import (CSV or pasted).

    Date window and link are checked per row in /import, so one bad row is
    reported instead of rejecting the whole batch.
    """

    message: str = Field(min_length=2, max_length=2000)
    # JSON key is `date`; named message_date here to avoid shadowing the type.
    message_date: Annotated[
        date | None, Field(validation_alias=AliasChoices("date", "message_date"))
    ] = None
    tags: list[str] = Field(default_factory=list, max_length=MAX_TAGS)
    source: str | None = Field(default=None, max_length=SOURCE_MAX)
    link: str | None = None

    model_config = {"populate_by_name": True}


class ImportRequest(BaseModel):
    """Body for POST /import — rows are parsed client-side (CSV or paste)."""

    rows: list[ImportRow] = Field(min_length=1, max_length=5000)


class LoginRequest(BaseModel):
    """Body for POST /auth/login."""

    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)


class UserCreateRequest(BaseModel):
    """Body for POST /users (admin only)."""

    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=8, max_length=200)
    role: str = Field(default="viewer")


class UserUpdateRequest(BaseModel):
    """Body for PATCH /users/{id} — change role and/or reset password."""

    role: str | None = None
    password: str | None = Field(default=None, min_length=8, max_length=200)
