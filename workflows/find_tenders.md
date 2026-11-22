# Workflow: Find matching government tenders

## Objective
Every day, find active Kenyan government tenders that a given small business is **eligible** for and would be a good fit for, then deliver the new ones (title, deadline, eligibility, reason, document link) to Telegram. The same pipeline powers the Streamlit app.

## Inputs
- A business profile, `profiles/<id>.yaml`: keywords, procurement categories, counties, `county_strict`, AGPO group (youth/women/pwd/null), NCA category, max bid security, min days to close.
- `.env` (or GitHub secrets): `GROQ_API_KEY`, and optionally `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

## Steps and tools
1. **Fetch:** `python -m tools.fetch_tenders`. It pages through `https://tenders.go.ke/api/active-tenders` (100 per page, 1 request/second) and stores allow-listed fields in `.tmp/tenders.db`.
2. **Match:** `python -m tools.match profiles/<id>.yaml`. The steps are rules → BM25 prefilter (top 25) → notice extraction (`tools/extract_notice.py`, cached by document URL, 4 parallel workers) → eligibility rules → one batched LLM call for ranking.
3. **Deliver:** `python -m tools.run_daily [--profile <id>] [--dry-run]`. It runs fetch, match for each profile and digest, sends only tenders not sent before for that profile, and records them in the `sent` table.
4. **Scheduled (optional, off by default since 2026-11-22):** `.github/workflows/daily.yml` runs step 3 on demand (`workflow_dispatch`). Uncomment its `schedule:` block for 06:00 EAT runs. Its `.tmp/` state persists through `actions/cache`. The owner doesn't want alerts, so keep it off unless they ask.
5. **Measure:** `python -m evals.extraction_eval` re-scores extraction against `evals/labels.json`. Run it after any prompt or model change.

## Rules that must not be broken
- **Personal data:** the API response includes procurement officers' ID numbers, phone numbers, personal emails and tokens (`created_by`, `team_members`, `bidders`). Only the `FIELDS` allow-list in `fetch_tenders.py` may leave `clean()`. Never write raw API responses to disk or logs. `tests/test_pipeline.py::test_no_personal_data_survives` enforces this.
- **No guessed eligibility:** a reservation (youth/women/PWD/AGPO) must be backed by a quote that `grounded()` finds in the notice text. Otherwise it becomes `unknown`, which stays in the results but is flagged.
- **The LLM never decides eligibility:** it extracts facts and scores fit. Python decides who may bid.

## Known quirks (learned 2026-11-21)
- `description`, `is_agpo` and `agpo_group_id` in the API are always null. Reservations appear only in notice PDFs.
- **Notices often list several tenders in one table** (NG-CDF, universities). The prompt names the tender reference, and `focus()` centres long texts on it.
- **PDF tables interleave columns,** for example "Reserved Monday 30th for Women". The model quotes with `...` gaps, and `grounded()` accepts fragments that appear in order within 300 characters.
- **"CATEGORY-12" in NG-CDF adverts is a pre-qualification category, not an NCA grade.** "Tender security of 2%" is not a KES amount, so `bid_security_kes` stays null.
- **Some notices are scanned images** with no text layer. The extractor falls back to the next document (max 2 downloads). OCR is not used, to stay free and light.
- **Full tender documents can be 100+ pages and several MB on a slow server.** Downloads are capped at 12 MB / 90 s, only the first 3 pages are read, and the eval uses a 600 s cap.
- **The portal keeps some tenders "active" after opening or award.** `past_bidding()` drops tenders that have opening minutes, evaluation, award or termination documents.
- **Most active tenders close within about a week** (316 of 495 on 2026-11-21), so daily runs matter.
- **The API rate-limits at 60 requests/minute** (`x-ratelimit-limit: 60`, with `Retry-After` on 429). A daily fetch needs only 6 requests, but heavy manual testing can trip it. `_get()` honours `Retry-After` and backs off 5/10/20/40 s.
- **There is no per-tender public URL** (the portal opens a modal), so digests link the notice PDF directly.

## Quota (Groq free tier)
- Extraction is the cost. Only the top 25 candidates per profile are read, results are cached, and only new tenders cost tokens on later days. A first run for 3 profiles uses about 50 extraction calls, and a typical daily run uses fewer than 15.
- Transient failures (rate limit, slow download) get one serial retry, aren't cached, and are retried on the next run.

## If something breaks
- **API shape changed** (KeyError in `clean()`): inspect one record with `curl ... | python -m json.tool`, update `clean()` and the test fixture, and keep the allow-list approach.
- **Telegram "chat not found":** message the bot first, then run `python -m tools.telegram` to list chat IDs.
- **Extraction accuracy drops:** run the eval, read the misses, adjust `SYSTEM` or `validate()`, and record the new lesson here.
