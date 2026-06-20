# Brahmavidya Path Checker

## Overview

Brahmavidya Path Checker is a FastAPI and PostgreSQL based application used to:

- Check existing Brahmavidya Paths
- Find similar paths based on word matching
- Add new paths
- Prevent duplicate entries

---

## Technology Stack

- Python 3.12+
- FastAPI
- PostgreSQL
- psycopg2-binary
- HTML, CSS, JavaScript
- Uvicorn

---

## Setup

### 1. Execute Database Script

Run the SQL script located in:

```text
sql/001_create_brahmavidya_paths.sql
```

### 2. Install Packages

```bash
pip install -r requirements.txt
```

### 3. Configure Database

Update `config.py` with PostgreSQL connection details.

---

## Run Application

```bash
python -m uvicorn app:app --reload
```

Application URL:

```text
http://localhost:8000
```

---

## Features

- Check existing Brahmavidya Paths
- Add new Brahmavidya Paths
- Duplicate prevention using normalized text
- Match percentage based on word overlap
- Gujarati text support

## Matching Algorithm

The application uses **Exact Word Matching**.

### How It Works

1. User enters a Brahmavidya Path.
2. Input text is normalized (trim spaces, lowercase).
3. Text is split into individual words.
4. Existing paths are also split into words.
5. Matching words are identified.
6. Match percentage is calculated.

### Match Percentage Formula

```text
Match Percentage =
(Number of Matching Words / Number of Input Words) × 100
```
