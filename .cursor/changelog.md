# Social Automation — Session Changelog

Reverse-chronological log of all agent work sessions. Each entry documents what was built, modified, or fixed.

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
