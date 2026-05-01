# social-automation — AI Agent Entry Point

Python FastAPI bot that auto-posts to Telegram / Facebook / Instagram, monitors comments, generates daily email reports, runs the geo-research pipeline, and orchestrates City Pulse cultural events. Runs on the I'M IN VPS (`135.125.254.131`) inside the imin-backend docker-compose stack as the `imin-bot` container.

## First action

If this is your first session in this project — read `~/Documents/imin-meta/AGENTS.md` FIRST. It covers all 7 repos.

Otherwise — start at `.cursor/rules/context-core.mdc`.

## Hard rules (apply every session)

1. Communicate with the user in **Ukrainian** (any product repo's `language.mdc`)
2. Before any commit or branch switch — `git status` (lost-work prevention)
3. **No politics, no Russia, Ukraine priority** — see `.cursor/rules/prompts-knowledge.mdc` Content rules
4. **Image priority**: real photo > Pexels > DALL-E. ALWAYS this order. Same for posts AND researcher
5. **`_clean_ai_meta()` post-processor** strips "Вибачте", "Як AI..." from outputs. Always run AI text through it
6. **`_publish_lock`** prevents duplicate publications across cron + catchup jobs
7. **APScheduler timezone** must be set explicitly to `Europe/Kyiv` on every job — without it cron fires in UTC

## Where things are

| Thing | Where |
|---|---|
| Entry point | `app/main.py` (FastAPI) |
| Scheduler | `scheduler/publish.py`, `scheduler/blog_sync.py` |
| Platform adapters | `platforms/{telegram,facebook,instagram,...}.py` |
| Researcher pipeline | `researcher/` |
| City Pulse | `city_pulse/` (orchestration, narration via ElevenLabs) |
| AI prompts | `ai/prompts.py` |
| DB models | `db/models.py` (SQLAlchemy + asyncpg) |
| Stats / reports | `stats/` |
| Token renewal | `stats/token_renewer.py` (FB + IG, 03:00 daily) |
| Backend client | `clients/imin_backend.py` |
| Config / env | `app/settings.py`, `.env` (server: `/opt/imin/.env`) |
| Cursor rules | `.cursor/rules/*.mdc` (~24 files) |
| Session changelog | `.cursor/changelog.md` |
| Cross-repo knowledge | `~/Documents/imin-meta/` |

## Deployment

The bot deploys to the same VPS as imin-backend. CI watches the `bot` branch on GitLab.

```bash
# From repo root
git add -A && git commit -m "msg"
git push gitlab main:bot       # triggers GitLab CI auto-deploy

# Or push to both remotes (GitLab + GitHub backup):
git push-all
```

CI rsyncs source to `135.125.254.131:/opt/imin-bot`, rebuilds the Docker image, restarts the `imin-bot` container.

Verify:

```bash
ssh root@135.125.254.131 "docker logs imin-bot --tail 30"
ssh root@135.125.254.131 "docker exec imin-bot curl -s http://localhost:8000/"
```

Manual fallback when CI is broken:

```bash
bash scripts/bot-deploy.sh 135.125.254.131
```

## Database

PostgreSQL `imin_bot` DB on the same Postgres container as `imin` (backend). Was SQLite on Railway; migrated 2026-04-30. No Alembic — tables auto-created by SQLAlchemy.

## Roles you might be in

For role-specific reading lists see `~/Documents/imin-meta/01-roles/`:

- `role-onboarder.md` — first session
- `role-bot-dev.md` — primary role for this repo
- `role-integrator.md` — when work calls or extends imin-backend endpoints
- `role-devops.md` — server / Docker work
- `role-architect.md` — big design decisions

## Task templates

For step-by-step recipes see `~/Documents/imin-meta/02-task-templates/`:

- `add-cross-repo-feature.md` — features that span bot + backend (or app)
- `deploy-release.md` — bot deploy section
- `investigate-bug.md` — failed publication, missing post, token expiry
- `add-translation-key.md` — adding 8-language strings

## End of session

- `git status` to verify nothing uncommitted
- Append entry to `.cursor/changelog.md`
- If change touched 2+ repos — append entry to `~/Documents/imin-meta/07-changelog/cross-repo-changelog.md` FIRST

## Daily schedule (Europe/Kyiv)

```
03:00  Renew Facebook + Instagram tokens (auto)
05:00  Health check
08:00  Post slot 0 (TG + FB + IG)
10:00  Post slot 1
12:00  Post slot 2
15:00  Post slot 3
18:00  Post slot 4
20:00  Daily report email
21:00  Blog sync to VPS

Every 5 min:   poll messages from all platforms
Every 6 min:   respond to pending messages (auto-reply)
Every 15 min:  catchup missed slots
Every 1 hour:  retry failed publications
Every 30 min:  health check
```

Each job uses `_publish_lock` and dedup checks to prevent races.

## Common commands

```bash
# Local dev
poetry install
poetry run uvicorn app.main:app --reload --port 8000

# Trigger publish manually (admin endpoint)
curl -X POST -H "X-API-Key: $ADMIN_API_KEY" \
  http://localhost:8000/api/trigger/publish/0

# Trigger daily report
curl -X POST -H "X-API-Key: $ADMIN_API_KEY" \
  http://localhost:8000/api/trigger/daily-report

# Trigger blog sync
curl -X POST -H "X-API-Key: $ADMIN_API_KEY" \
  http://localhost:8000/api/trigger/blog-sync

# Today's publications
curl -H "X-API-Key: $ADMIN_API_KEY" \
  http://localhost:8000/api/debug/publications
```
