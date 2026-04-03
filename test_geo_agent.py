"""One-off test: simulate geo-research cycle for today's posts."""
import asyncio
import json
import uuid
from datetime import datetime, timezone

async def main():
    import os
    os.environ.setdefault("DATA_DIR", "/tmp/geo-test-data")

    from db.database import init_db, async_session
    from db.models import GeoResearchTask, GeoResearchStatus
    from geo_agent.researcher import research_location

    await init_db()

    today_posts = [
        {"lat": -20.2833, "lon": 57.4333, "name": "Ланкеві, Маврикій"},
        {"lat": 32.0809, "lon": -81.0912, "name": "Savannah, Georgia, USA"},
    ]

    for post in today_posts:
        request_id = str(uuid.uuid4())
        received_at = datetime.now(timezone.utc)

        print(f"\n{'='*70}")
        print(f"[QUEUE] Отримано геодані по API:")
        print(f"  request_id: {request_id}")
        print(f"  lat: {post['lat']}, lon: {post['lon']}")
        print(f"  name: {post['name']}")
        print(f"  received_at: {received_at.isoformat()}")

        async with async_session() as session:
            task = GeoResearchTask(
                request_id=request_id,
                latitude=post["lat"],
                longitude=post["lon"],
                name=post["name"],
                language="uk",
                status=GeoResearchStatus.QUEUED,
                received_at=received_at,
            )
            session.add(task)
            await session.commit()
            task_id = task.id

        print(f"  status: QUEUED")

        async with async_session() as session:
            db_task = await session.get(GeoResearchTask, task_id)
            db_task.status = GeoResearchStatus.PROCESSING
            await session.commit()

        print(f"\n[PROCESSING] AI досліджує {post['name']}...")

        try:
            result = await research_location(
                latitude=post["lat"],
                longitude=post["lon"],
                name=post["name"],
                language="uk",
            )
            completed_at = datetime.now(timezone.utc)

            async with async_session() as session:
                db_task = await session.get(GeoResearchTask, task_id)
                if result:
                    db_task.status = GeoResearchStatus.COMPLETED
                    db_task.result = json.dumps(result, ensure_ascii=False)
                else:
                    db_task.status = GeoResearchStatus.EMPTY
                db_task.completed_at = completed_at
                await session.commit()

            print(f"\n[COMPLETED] Результат готовий до передачі по API:")
            print(f"  completed_at: {completed_at.isoformat()}")
            print(f"  processing_time: {(completed_at - received_at).total_seconds():.1f}s")

            if result:
                print(f"\n  --- SUMMARY ---")
                print(f"  {result['summary'][:500]}...")

                print(f"\n  --- HISTORY ({len(result.get('history', []))} entries) ---")
                for h in result.get("history", [])[:5]:
                    print(f"  [{h['period']}] {h['description'][:100]}")

                print(f"\n  --- PLACES ({len(result.get('places', []))} entries) ---")
                for p in result.get("places", [])[:5]:
                    url = f" | {p['url']}" if p.get("url") else ""
                    print(f"  [{p['type']}] {p['name']}: {p['description'][:80]}{url}")

                print(f"\n  --- NEWS ({len(result.get('news', []))} entries) ---")
                for n in result.get("news", [])[:3]:
                    print(f"  {n['title']}: {n['description'][:100]}")

                total_chars = len(json.dumps(result, ensure_ascii=False))
                print(f"\n  Total result size: {total_chars} chars (~{total_chars/4000:.1f} pages)")
            else:
                print(f"  status: EMPTY (нічого не знайдено)")

        except Exception as e:
            print(f"\n[FAILED] {e}")

    print(f"\n{'='*70}")
    print(f"\n[STATUS] Готові до видачі по GET /api/geo-research/completed")

    async with async_session() as session:
        from sqlalchemy import select
        rows = (await session.execute(
            select(GeoResearchTask).order_by(GeoResearchTask.id)
        )).scalars().all()
        for r in rows:
            print(f"  {r.request_id} | {r.name} | {r.status.value} | received: {r.received_at} | completed: {r.completed_at}")


if __name__ == "__main__":
    asyncio.run(main())
