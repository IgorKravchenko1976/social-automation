# Social Automation — Session Changelog

Reverse-chronological log of all agent work sessions. Each entry documents what was built, modified, or fixed.

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
