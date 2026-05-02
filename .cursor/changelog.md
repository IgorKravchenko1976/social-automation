# Social Automation — Session Changelog

Reverse-chronological log of all agent work sessions. Each entry documents what was built, modified, or fixed.

---

## 2026-05-02 — Fix: SVG-as-jpg loop + handoff bursts blocking regular slots

**Branch**: feature/poi-handoff-consumer (worktree: main checkout)
**Scope**: media validation / POI handoff / slot accounting
**Triggering symptom**: zero TG/FB/IG posts published for the 08:00, 10:00 and 12:00 slots; the same broken `Макдональдз — Kyiv` POI hand-off cycled hourly with `IMAGE_PROCESS_FAILED` / `Invalid parameter` / `Could not get public URL`.

### Root cause analysis (3 systemic bugs, not 1)

1. **`download_image_from_url` accepted SVG (and tiny) payloads.** Wikidata's `imageUrl` for McDonald's was a 389-byte SVG logo. Content-type `image/svg+xml` matched the lax `image/` filter, file was stored as `poi_*.jpg`, then every social platform rejected it. POI handoff reported `failed_transient`, backend handed the SAME POI back next hour → infinite SVG retry loop.
2. **`count_published_today()` counted POI hand-off posts as if they were regular slots.** After 4 handoff posts (incl. 2 broken McDonald's), `published_count >= 4` made `_publish_scheduled_post_inner` SKIP every regular slot for the day (`Slot 2 SKIPPED: already 4 post(s) published today`). Slots 0/1/2/3/4 (web_news/leisure/feature) never ran.
3. (Pre-existing, unchanged) `CATCHUP === No missed slots (published=4, past_slots=3)` masks the real problem because it sums all PUBLISHED posts instead of checking each slot's queue.

### Fixes (systemic, not local)

#### `content/media.py` — strict image validation

- Reject `Content-Type` containing `svg`.
- Floor: `_MIN_IMAGE_BYTES = 5 KB` (real landscape photos are ≥5 KB; logos / 1×1 placeholders are not).
- Magic-byte check: only `JPEG (FF D8 FF)`, `PNG (89 50 4E 47 ...)`, `WebP (RIFF ... WEBP)` accepted; anything else → reject. Stored extension is now derived from the magic bytes, not the (often-wrong) URL extension.
- Result: McDonald's SVG path now returns `None` → publisher sees no `image_path` → POI is published text-only on TG+FB; IG is skipped with `instagram_requires_image` (a permanent reason already in `_PERMANENT_FAILURE_MARKERS`). At least one platform succeeds → `_try_publish_post` returns True → handoff reported as `published` → backend marks POI posted → no more loop.

#### `db/models.py` + `db/database.py` — track handoff origin on Post

- New nullable column `posts.handoff_id INTEGER` (auto-applied by `_run_migrations` ALTER TABLE on next start; safe — Postgres supports `IF NOT EXISTS`).
- Stores the backend `social_post_handoff.id` for posts created from the hand-off pipeline; NULL for regular scheduled-slot posts.

#### `scheduler/post_creator.py` + `scheduler/city_pulse_post_creator.py` — write handoff_id

- `prepare_local_post_for_poi(poi, handoff_id=...)` and `prepare_local_post_for_event(event, handoff_id=...)` now accept and persist `handoff_id`. Default `None` keeps the legacy `process_city_pulse_post()` path unchanged.

#### `scheduler/city_pulse_handoff_publisher.py` — pass through handoff_id

- Both `_process_handoff_item` (city_pulse) and `_process_poi_handoff` (POI) now forward `item.handoff_id` to the post-creator helpers.

#### `scheduler/publisher.py` — exclude handoff posts from slot accounting

- `count_published_today()` now adds `Post.handoff_id IS NULL` to the WHERE clause. Result: handoff bursts (POI / city_pulse) no longer "consume" a slot's quota. The 5 daily slots run on their own schedule regardless of how many handoff posts published that day.

### Why this is a one-shot fix, not a band-aid

- Even if a future POI source returns another bad image (HEIC, AVIF, broken JPEG header), the magic-byte check rejects it before the file ever reaches the platform adapters.
- Even if a handoff cycle publishes 20 POIs in one hour, the 5 regular slots still fire because the accounting filter is structural, not heuristic.
- No DB-level workarounds, no "dedup the McDonald's title" hacks.

### Operational steps after deploy

1. On prod, mark the two stuck QUEUED posts (454, 455) FAILED so the publisher loop unblocks.
2. Restart the bot container so the new column is added by `_run_migrations` and the new code is loaded.
3. Trigger `publish_missed_slots()` to backfill today's regular slots that were skipped.

### Follow-up: classify handoff failures (don't blanket-report failed_transient)

`scheduler/city_pulse_handoff_publisher.py` previously reported `failed_transient` for every failed publication. The backend then auto-retried the same row up to `SocialMaxAttempts=3` times (~3 hours of useless cycles) before promoting to permanent. New `_classify_publication_failure()` reads each Publication's status + error_message and:

- Returns `failed_transient` if any pub is still QUEUED/PUBLISHING (publish loop didn't reach a verdict) or any FAILED pub has a network/timeout/token/daily-cap reason.
- Returns `failed_permanent` only when EVERY non-published pub failed with a structural reason matched by `_STRUCTURAL_FAILURE_MARKERS` (image_process_failed, invalid parameter, could not get public url for image, fact-check rejected, instagram requires image, no real photo, etc.).

Result: a POI whose only image is broken / fact-check-fails / etc. is now permanently dropped on the FIRST failed cycle instead of cycling 3 times. Network glitches still get retried. Backend's auto-promote-after-3 stays as a safety net.

---

## 2026-05-02 — POI hand-off consumer (separate cadence + lock)

**Branch**: main (MR !16) → bot (MR !17), pipeline 2494820712 success
**Scope**: poi / publisher / scheduler

### What was done
- Added a POI consumer to the hand-off pipeline alongside the
  existing city_pulse one. Uses a SEPARATE APScheduler job,
  separate `asyncio.Lock`, separate client_id ('social-bot-poi')
  so the two channels never block each other.
- Cadence: every 60 min, batch=1. Backend anti-burst (-5 score
  for same `point_type` in same city in last 6h) prevents flood.
- Verified end-to-end: handoff #20 → POI 36123 (`Garden`, cafe,
  Kyiv) published as post 452 in ~20s, backend stamped
  `map_point_details.posted_to_social_at = 07:58:23 UTC`.

### Files created
- `scheduler/poi_handoff_publisher.py` — `publish_poi_via_handoff`
  job (separate lock, batch=1, client_id='social-bot-poi').

### Files modified
- `scheduler/handoff_client.py` — `next_batch()` accepts
  `kind='city_event'|'poi'|'*'`. New `fetch_poi_payload()` calls
  `GET /v1/api/research/poi/{id}/for-post`.
- `scheduler/post_creator.py` — public `prepare_local_post_for_poi`
  helper (mirror of `prepare_local_post_for_event`). Single source
  of truth for POI Post + Publication structure.
- `scheduler/city_pulse_handoff_publisher.py` — dispatches
  `source_kind='poi'` to the new `_process_poi_handoff` path.
- `config/settings.py` — `USE_HANDOFF_API_POI` feature flag (default
  true, independent from `USE_HANDOFF_API` for fine-grained
  rollback).
- `main.py` — register the new 60-min POI job behind both flags.

### Coexistence with legacy POI publisher
- Legacy `_create_poi_spotlight_post` (slot 2 + web_news fallbacks)
  remains active. Both paths share
  `map_point_details.posted_to_social_at` so each POI is posted at
  most once across both channels.

### AI-budget separation (per user request)
- `poi_handoff_publisher.py` header explicitly notes: this code
  ONLY orchestrates which already-prepared POI to publish next.
  All description / translation / photo enrichment happened
  upstream in `geo_agent/*` and `datacollector/*` pipelines that
  have their own queues and shouldn't share rate-limit budget
  with bursty publishing.

### Rollback
```
echo 'USE_HANDOFF_API_POI=false' >> /opt/imin-bot/.env
docker compose restart imin-bot
```

---

## 2026-05-02 — Region research: richer prompt for accurate, fresh, detailed descriptions

**Branch**: main (PR #12 merged), pending push to bot
**Scope**: geo_agent / region_researcher

### Why
Phase 4 of the france-shows-ukraine fix (paired with imin-backend MR !238 which makes admin_regions the canonical source for Explorer descriptions on every level). Without prompt rework the bot would still emit 2-sentence stubs and leave the new Explorer "Деталі" tab feeling empty for continents and most countries.

### What was done
- `geo_agent/region_researcher.py` REGION_RESEARCH_PROMPT rewritten:
  - description target: ~500-700 chars, structured on history / culture / nature; **350-char minimum** so the model can no longer return a placeholder.
  - three explicit priorities in order — accuracy → freshness → traveler relevance.
  - continent rivers explicitly supported.
  - bans fabricated dates / figures, demands at least one modern data point alongside any 20th-century reference.
- Tuning: max_tokens 1500 → 2000, temperature 0.4 → 0.5, Wikipedia context cap 2000 → 4000 chars (so the model has enough material to honor the wider description budget).

### Deploy
- PR #12 merged to GitHub main.
- TODO: push main → gitlab/bot to roll out to the running bot container.

### Pending follow-ups
- Phase 5: lat/lon → country sanity-check inside `AddressService.ResolveChain` to delete and recreate corrupted chain nodes when Nominatim disagrees with the DB (different MR / repo).
- Background job to refresh empty `admin_regions.summary` rows for continents and tourist countries.

---

## 2026-05-02 — Consume backend social hand-off API (city_pulse)

**Branch**: main (MR !14) → bot (MR !15), pushed to gitlab + github
**Scope**: city-pulse / publisher / scheduler

### What was done
- Switched the City Pulse pipeline from "bot polls one event +
  maintains its own QUEUED state" to a hand-off contract where the
  backend hands out batched, deduplicated, leased work-items.
- Eliminated the 5-min creator + 15-min publisher pair — now ONE
  job every 15 min that fetches a 3-event batch, publishes them,
  and reports each result back to the backend.
- Root cause we're fixing: incident 2026-05-02 "no posts for 9h",
  where `retry_failed_publications` kept resurrecting Instagram
  posts that had no real photo and burned 100% of the publish
  slots on stale rows. The new design makes the backend reject
  those at the queue-build stage, so they can never reach the bot.

### Files created
- `scheduler/handoff_client.py` — thin httpx wrapper around
  `GET /v1/api/social/next-batch` and `POST /v1/api/social/report-result`,
  with strict result-string validation.
- `scheduler/city_pulse_handoff_publisher.py` — `publish_via_handoff`:
  claim → fetch payload → reuse `prepare_local_post_for_event` →
  reuse existing `_try_publish_post` for fact-check + dispatch →
  close lease.

### Files modified
- `scheduler/city_pulse_post_creator.py` — extract
  `prepare_local_post_for_event` so the legacy creator AND the
  hand-off publisher share the same Post-creation code path.
- `config/settings.py` — `USE_HANDOFF_API` feature flag (default true).
- `main.py` — register the hand-off publisher when flag is on,
  fall back to the legacy two-job pipeline otherwise.

### AI-budget separation (per user request)
- `handoff_client.py` and `city_pulse_handoff_publisher.py` headers
  document explicitly that this code only orchestrates which
  ALREADY-PREPARED row the bot publishes next. It does NOT touch
  Perplexity / OpenAI on behalf of the long-form researcher in
  `geo_agent/*` — researcher and fixer have their own queues, so
  a bursty social run can't starve them even when keys are shared.

### Status
- [x] Deployed via GitLab CI deploy_bot pipeline 2494760966 success
- [x] `imin-bot` confirmed running new code:
      `[city-pulse] Hand-off publisher every 15 min (backend-driven queue, batch=3)`
- [ ] First publish cycle observation pending (~15 min after boot)

### Rollback
```
echo 'USE_HANDOFF_API=false' >> /opt/imin-bot/.env
docker compose restart imin-bot
```

---

## 2026-05-01 — Knowledge architecture: AGENTS.md + imin-meta cross-repo knowledge base

**Branch**: perf/enrich-faster-cycle (commit `31f4ab7`, pushed to gitlab + github)
**Scope**: docs / agent-onboarding

### What was done

- Created `AGENTS.md` at repo root as the standard Cursor agent entry point (bot-aware: deploy via `bot` branch CI, daily schedule, common commands)
- Updated `.cursor/rules/context-core.mdc` to reference new `imin-meta` repo for cross-repo concerns (api-contracts, glossary, ADRs)
- Helped build `~/Documents/imin-meta/` — a new 7th repo at gitlab.com/igork2011/imin-meta for cross-repo knowledge
- Added 4 synced shared rules: datetime-standard, no-russian-domains, release-news-workflow, website-deploy (canonical lives in imin-meta, drift detected via sha256)

### Files created

- `AGENTS.md`
- `.cursor/rules/datetime-standard.mdc` (sync stub with CANONICAL header)
- `.cursor/rules/no-russian-domains.mdc` (sync stub)
- `.cursor/rules/release-news-workflow.mdc` (sync stub)
- `.cursor/rules/website-deploy.mdc` (sync stub)

### Files modified

- `.cursor/rules/context-core.mdc` — added "First-time agent? Cross-repo work?" section pointing to imin-meta

### Status

- [x] Completed and pushed to both remotes (gitlab + github backup)

### How to test

- `cat AGENTS.md` — confirm bot entry point
- Open `.cursor/rules/datetime-standard.mdc` — top shows CANONICAL header from imin-meta

### Related rules updated

- `context-core.mdc` — imin-meta references
- Cross-repo: `~/Documents/imin-meta/07-changelog/cross-repo-changelog.md` — entry written

---
