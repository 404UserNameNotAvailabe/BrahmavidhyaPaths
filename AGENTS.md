# Working rules — BrahmavidhyaPaths (backend)

These rules are for anyone changing this repo, people and AI coding tools alike. Read `README.md` first: it explains the domain (seasons, links, check logic).

## Data is sacred text
- `message_text` is the guru's words. Never "clean up", translate, re-punctuate or reformat it.
- The archive is live production data on Neon. Before any write, run a **read-only count first**, and do the write **inside a transaction** that checks the row count before committing.
- Never invent a date, a link or a path. Original links come from baps.org season pages only (see README). Their filenames are irregular, so never guess one.

## Keep the three copies of a rule in sync
The Dhanurmas window (16 Dec – 14 Jan) is enforced in `Backend/taxonomy.py`, in `Backend/sql/schema.sql` (`ck_messages_dhanurmas`), and in the frontend's `src/lib/season.ts`. The same goes for the link format (`clean_link` / `ck_messages_link` / `isValidLink`). Change one, change all three.

## Schema
- `Backend/sql/schema.sql` runs on **every API start**. Everything in it must be idempotent and safe to run against the live DB.
- Test a schema change against the real DB in a transaction and `ROLLBACK`, before merging.
- Generated columns (`normalized_text`, `year`, `season`) are computed by Postgres. Don't set them in code.
- One-off data changes belong in their own file under `Backend/sql/` (e.g. `links_baps.sql`), not in `schema.sql`.

## API changes
- The frontend (`brahmavidya-insights`) consumes this API. Any field rename or removal needs a matching frontend PR, and **the backend must deploy first**.
- Keep `Backend/SETUP.md`'s API table up to date.
- Never return exception text to the client. Use `fail()`, which logs server-side.

## Secrets
- `.env`, service-account JSON files and Neon credentials never go in git (`.gitignore` covers them). Use `Backend/.env.example` for new settings.

## Git
- Branch per change and open a PR into `main`. There is no CI: run `python -c "import app"` with the venv, plus a quick model/endpoint check, before opening it.
- Commit messages carry no AI attribution trailers.
