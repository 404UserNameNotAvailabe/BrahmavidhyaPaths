from fastapi import FastAPI
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from database import get_connection
from database import return_connection

import re

app = FastAPI(
    title="Brahmavidya Path Checker"
)

app.mount(
    "/static",
    StaticFiles(directory="static"),
    name="static"
)

templates = Jinja2Templates(
    directory="templates"
)


def normalize_text(text: str) -> str:
    """
    Normalize text for storage and comparison.
    """

    text = text.strip()

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.lower()


def tokenize(text: str) -> set[str]:
    """
    Convert text into unique words.

    Example:
    'રાજીપો એ જ મોક્ષ.'
    =>
    {'રાજીપો', 'એ', 'જ', 'મોક્ષ'}
    """

    text = normalize_text(text)

    text = re.sub(
        r"[^\w\s઀-૿]",
        "",
        text
    )

    return set(text.split())


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request
        }
    )


@app.post("/check")
async def check_path(payload: dict):

    input_path = payload.get("path", "")

    if not input_path:
        return {
            "success": False,
            "message": "Path is required"
        }

    input_path = input_path.strip()

    if len(input_path) < 2:
        return {
            "success": False,
            "message": "Minimum 2 characters required"
        }

    input_tokens = tokenize(input_path)

    if not input_tokens:
        return {
            "success": False,
            "message": "No valid words found"
        }

    conn = get_connection()

    try:

        cur = conn.cursor()

        cur.execute("""
            SELECT
                Id,
                PathText,
                NormalizedText
            FROM BrahmavidyaPaths
        """)

        rows = cur.fetchall()

        matches = []

        for row in rows:

            path_id = row[0]
            path_text = row[1]
            normalized_text = row[2]

            existing_tokens = tokenize(
                normalized_text
            )

            matched_words = list(
                input_tokens.intersection(
                    existing_tokens
                )
            )

            if not matched_words:
                continue

            match_percentage = round(
                (
                    len(matched_words)
                    / len(input_tokens)
                ) * 100,
                2
            )

            matches.append({
                "id": path_id,
                "pathText": path_text,
                "matchedWords": matched_words,
                "matchPercentage": match_percentage
            })

        matches.sort(
            key=lambda x:
            x["matchPercentage"],
            reverse=True
        )

        return {
            "success": True,
            "inputPath": input_path,
            "totalMatches": len(matches),
            "matches": matches[:20]
        }

    finally:
        return_connection(conn)


@app.post("/add")
async def add_path(payload: dict):

    path_text = payload.get("path", "")

    if not path_text:
        return {
            "success": False,
            "message": "Path is required"
        }

    path_text = path_text.strip()

    if len(path_text) < 2:
        return {
            "success": False,
            "message": "Minimum 2 characters required"
        }

    normalized_text = normalize_text(
        path_text
    )

    conn = get_connection()

    try:

        cur = conn.cursor()

        cur.execute("""
            SELECT 1
            FROM BrahmavidyaPaths
            WHERE NormalizedText = %s
        """, (normalized_text,))

        exists = cur.fetchone()

        if exists:

            return {
                "success": False,
                "message": "Path already exists"
            }

        cur.execute("""
            INSERT INTO BrahmavidyaPaths
            (
                PathText,
                NormalizedText
            )
            VALUES
            (
                %s,
                %s
            )
        """,
        (
            path_text,
            normalized_text
        ))

        conn.commit()

        return {
            "success": True,
            "message": "Path added successfully"
        }

    finally:
        return_connection(conn)