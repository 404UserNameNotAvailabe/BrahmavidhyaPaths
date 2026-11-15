# Candidates — how it works and why

A pool of sentences proposed for coming seasons, each checked against every path already given, by **words**, **meaning** and an **AI judge**, with a shortlist → approve → give flow. Build spec: "Brahmavidya Candidates: build spec for the development agent" (14 Nov 2026).

## The check (one candidate)
1. **Words** (`candidate_rules.py`): compare with all archive paths, using a comparison key that ignores spaces and punctuation, plus word coverage and the longest shared phrase. `exact` / `contained` (≥3 words) means **Already given**, saved with **no AI call**.
2. **Meaning**: embed the candidate (`gemini-embedding-001`, `RETRIEVAL_QUERY`) and take the 8 nearest paths. Shortlist = every word hit + those 8, capped at 12. No similarity threshold decides anything.
3. **AI judge** (`judge.py`): one Gemini call, temperature 0, enforced JSON. Labels each shortlisted path `same_sentence` / `same_message` / `similar` / `different` with a reason of 25 words or fewer, and picks 1–2 topics. Replies are validated strictly and retried once. After that the candidate is **Check failed**, never a silent "new".
4. **Result** (first rule wins; the AI can raise a result but never lower one):

| Result | Rule |
|---|---|
| Already given | word `exact`/`contained`, or judge `same_sentence` |
| Same message | judge `same_message` |
| Similar, review | judge `similar`, or word `strong`/`some` |
| New | none of the above |

The worker (`candidate_worker.py`) uses `candidates.check_state` as its queue, so a restart loses nothing. No request ever waits on a model. A result is saved only if the text wasn't edited mid-check.

## Decisions taken (change any of them in one place)
| Decision | Choice | Where |
|---|---|---|
| Where candidates live | own tables (`candidates`, `candidate_matches`, `topics`) | `sql/schema.sql` |
| Results shown | 4 labels + shared-word numbers + a reason; never a % for meaning | spec |
| Topics | the team's 25 Vishay, stored as tag values; 1–2 per path | `sql/seed_topics.sql` |
| Approval | editor: pending ↔ shortlisted; admin: approve / reject (reject needs a note unless already given). Once approved or rejected, only an admin changes it. Approve needs a finished check and is refused for "Already given". Editors mark an approved candidate as given | `candidates_api.set_candidate_status` |
| No AI configured | a candidate the words don't settle fails at once ("AI check is not configured"), never waits on retries, never becomes "New" | `candidates.assess` |
| AI reply slightly off | a reason over 25 words is trimmed and a third topic dropped (not a failure); wrong ids, labels or topics fail and retry once | `judge.validate` |
| AI | Vertex AI (private, paid tier), model from `GEMINI_JUDGE_MODEL`, chosen by `scripts/eval_candidates.py` | env |
| Daily cap | `JUDGE_DAILY_CALL_LIMIT=300` AI-judged checks per India day (first load ≈ 160) | env |
| Duplicate key | punctuation-free on both `messages` and `candidates` (v2, Nov 2026) | `schema.sql` |
| Approved for | the upcoming season by default (`taxonomy.upcoming_season`) | API |
| Mobile nav | the app's existing menu, not the mockup's bottom bar (spec) | frontend |
| Season plan | later phase (`plan_date` column exists) | — |

## Privacy
Only the candidate text, up to 12 archive paths (public on baps.org) and the topic names are sent to Vertex AI. No usernames, notes or ids other than path ids. Logs carry the candidate id, duration and outcome, never text.

## Run / test
- Unit tests: `cd Backend && python3 -m unittest discover -s tests -t .`
- Integration tests (throwaway DB only — they refuse any host that isn't local or a DB not named `bvtest*`): see `tests/test_integration.py` docstring.
- Accuracy: `python -m scripts.eval_candidates sheet.csv --model <model>`.
- Archive topics: `python -m scripts.backfill_topics --dry-run`, then for real after the team agrees.
