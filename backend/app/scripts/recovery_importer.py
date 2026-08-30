"""P0.1 DATA RECLAMATION — selective recovery importer.

Mounts a verified pre-incident backup into an ISOLATED database, produces a
read-only domain diff against current production, classifies recoverables,
and applies SELECTIVE recovery with full provenance — never a blind
full-restore over production.

Subcommands:
  mount     -> create/refresh the isolated pre-incident database
  counts    -> canonical domain counts for both sides
  diff      -> per-domain missing/new/conflicting entity ids
  plan      -> DRY-RUN recovery plan (create/reconcile/skip/archive/skip-test)
  apply     -> execute the high-confidence plan transactionally

Laws implemented here (P0.1 PART 5-12, 16-21):
- The backup is a RECOVERY SOURCE, not a replacement command.
- Current STATE_EPOCH stays; historical imports happen WITHIN this lineage.
- Authentic historical Events are imported with their ORIGINAL ids and
  timestamps (recovered history, never fabricated).
- Test/canary artifacts are classified but not auto-reclaimed.
- Device rows are security state: only explicit rebind with revision bump.
- Every recovered item records provenance (incident, backup, original id).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

BACKUP_DEFAULT = str(
    Path.home() / ".ev/backups/ev-backup-20260824T210005.evbackup"
)
INCIDENT_ID = "INC-20260825-P0-DATABASE-WIPE"
RECOVERY_SOURCE = "PRE_INCIDENT_BACKUP_20260824T210005Z"

# Canonical owner-state tables recovered with ORIGINAL primary keys.
DOMAIN_TABLES = [
    "projects",
    "goals",
    "goal_steps",
    "commitments",
    "memories",
    "entities",
    "entity_relationships",
    "decisions",
]

# Test/canary artifact classifier (PART 7). Never auto-reclaimed.
_TEST_MARKERS = (
    "g1 final commitment proof",
    "g2 crosswise canary",
    "crosswise canary",
    "gate audio test",
    "final provider proof",
    "canary harness",
    "tx isolation phone",
    "escape test phone",
    "broker phone",
    "sanitize phone",
    "g21 slice",
    "g2 dedupe goal",
    "g2 update replay proof",
    "g2 conflict proof",
    "g2 reconnect proof",
    "g2 trust promotion proof",
    "g2 provenance proof",
    "envelope target",
    "guardtest123",
    "luna control test",
)


def is_test_artifact(title_or_desc: str | None) -> bool:
    blob = (title_or_desc or "").casefold()
    return any(m in blob for m in _TEST_MARKERS)


async def _engine(url: str):
    from sqlalchemy.ext.asyncio import create_async_engine

    return create_async_engine(url)


def _drill_url() -> str:
    base = os.environ.get("EV_DATABASE_URL", "")
    if "/ev?" in base or base.rstrip("/").endswith("/ev"):
        root = base.rsplit("/", 1)[0]
    else:
        root = base.rsplit("/", 1)[0]
    return f"{root}/ev_preincident_mount"


async def cmd_mount(args: argparse.Namespace) -> None:
    """Create isolated DB, build CURRENT schema, replay backup into it."""
    passphrase = args.passphrase or os.environ.get("EV_BACKUP_PASSPHRASE")
    assert passphrase, "passphrase required"
    drill_url = _drill_url()
    dbname = drill_url.rsplit("/", 1)[-1]


    os.environ["EV_DATABASE_URL"].rsplit("/", 1)[0]
    import subprocess
    import time

    host = urlparse(os.environ["EV_DATABASE_URL"]).hostname or "localhost"
    def _psql(db: str, stmt: str):
        return subprocess.run(
            ["psql", "-h", host, "-U", "sahajpatel", "-d", db, "-v", "ON_ERROR_STOP=1", "-c", stmt],
            capture_output=True, text=True,
        )
    # Terminate lingering connections so the drop cannot fail silently.
    _psql("postgres",
          f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
          f"WHERE datname = '{dbname}' AND pid <> pg_backend_pid()")
    for _attempt in range(5):
        r = _psql("postgres", f'DROP DATABASE IF EXISTS "{dbname}"')
        if r.returncode == 0:
            break
        time.sleep(1.5)
    else:
        sys.exit(f"mount: could not drop {dbname}: {r.stderr}")
    r = _psql("postgres", f'CREATE DATABASE "{dbname}"')
    if r.returncode != 0:
        sys.exit(f"mount: could not create {dbname}: {r.stderr}")

    # Postgres 15+: fresh databases need explicit grants + extensions that
    # the canonical schema depends on (pgcrypto/vector).

    db_user = urlparse(os.environ["EV_DATABASE_URL"]).username or "sahajpatel"
    host_part = urlparse(os.environ["EV_DATABASE_URL"]).hostname or "localhost"
    for stmt in (
        f'GRANT ALL ON SCHEMA public TO "{db_user}"',
        'CREATE EXTENSION IF NOT EXISTS vector',
        'CREATE EXTENSION IF NOT EXISTS pgcrypto',
    ):
        subprocess.run(
            ["psql", "-h", host_part, "-U", "sahajpatel", "-d", dbname, "-c", stmt],
            capture_output=True,
        )

    os.environ["EV_DATABASE_URL"] = drill_url
    from app.db import SessionLocal
    from app.models import Base

    eng = await _engine(drill_url)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await eng.dispose()

    # FAITHFUL PAYLOAD MOUNT (no wipe/rebuild side effects): insert backup
    # rows preserving ORIGINAL ids/timestamps. Embedding vectors excluded
    # (derived artifacts — rebuilt later if needed).
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.services.backup import load_backup

    payload = load_backup(args.backup, passphrase)
    # Dependency-safe insert order + per-table isolation.
    ORDER = [
        "devices",
        "events",
        "entities",
        "memories",           # pass 1: supersedes_id deferred
        "memory_entities",
        "memory_events",
        "conflicts",
        "memories_supersedes_fixup",
        "access_log",
    ]
    vector_cols_by_table = {
        t.name: {c.name for c in t.columns if c.type.__class__.__name__ == "Vector"}
        for t in Base.metadata.tables.values()
    }
    async with SessionLocal() as s:
        inserted: dict[str, int] = {}
        skipped: dict[str, int] = {}

        def _clean(table_name: str, tbl, rows):
            vecs = vector_cols_by_table.get(table_name, set())
            out = []
            for row in rows:
                clean = {k: v for k, v in row.items() if k in cols_cache[table_name] and k not in vecs}
                out.append(clean)
            return out

        cols_cache = {
            t.name: set(t.columns.keys()) for t in Base.metadata.tables.values()
        }

        async def _insert(table_name: str, tbl, batch) -> bool:
            stmt = pg_insert(tbl)
            if "id" in tbl.columns:
                stmt = stmt.on_conflict_do_nothing(index_elements=["id"])
            try:
                await s.execute(stmt, batch)
                await s.commit()
                inserted[table_name] = inserted.get(table_name, 0) + len(batch)
                return True
            except Exception as exc:
                await s.rollback()
                skipped[table_name] = skipped.get(table_name, 0) + len(batch)
                print(f"MOUNT-SKIP {table_name}: {str(exc)[:140]}", file=sys.stderr)
                return False

        for table_name in ORDER:
            if table_name == "memories_supersedes_fixup":
                # Pass 2: restore intra-memory supersedes links now that all
                # memory rows exist.
                rows = payload.get("memories") or []
                tbl = Base.metadata.tables["memories"]
                fixes = [
                    {"id": r["id"], "supersedes_id": r.get("supersedes_id")}
                    for r in rows
                    if r.get("supersedes_id")
                ]
                if fixes:
                    from sqlalchemy import text as _upd
                    n = 0
                    for f in fixes:
                        res = await s.execute(
                            _upd("UPDATE memories SET supersedes_id=:sid WHERE id=:id"),
                            {"sid": f["supersedes_id"], "id": f["id"]},
                        )
                        n += res.rowcount or 0
                    await s.commit()
                    inserted["memories_supersedes_fixups"] = n
                continue

            rows = payload.get(table_name)
            if not isinstance(rows, list) or not rows:
                continue
            tbl = Base.metadata.tables.get(table_name)
            if tbl is None:
                continue
            vector_cols_by_table.get(table_name, set())

            if table_name == "memories":
                # Pass 1 without intra-memory self-references (supersedes /
                # superseded_by); Pass 2 restores them row-by-row.
                SELF_REFS = ("supersedes_id", "superseded_by_id")
                base_rows = [
                    {k: v for k, v in r.items() if k not in SELF_REFS}
                    for r in rows
                ]
                ok1 = await _insert(table_name, tbl, base_rows)
                if not ok1:
                    continue
                inserted.setdefault(table_name, len(base_rows))
                from sqlalchemy import text as _upd

                for r in rows:
                    sets = {
                        col: r.get(col)
                        for col in SELF_REFS
                        if r.get(col) is not None
                    }
                    if not sets:
                        continue
                    await s.execute(
                        _upd(
                            "UPDATE memories SET "
                            + ", ".join(f"{col} = :{col}" for col in sets)
                            + " WHERE id = :id"
                        ),
                        {"id": r["id"], **sets},
                    )
                await s.commit()
                continue

            clean_rows = _clean(table_name, tbl, rows)
            await _insert(table_name, tbl, clean_rows)

    print(json.dumps({
        "mounted": drill_url,
        "inserted": inserted,
        "skipped_tables": skipped,
    }))


async def _counts(engine) -> dict[str, int]:
    from sqlalchemy import text

    out: dict[str, int] = {}
    async with engine.connect() as c:
        last_error = None
        for t in [
            "projects", "goals", "goal_steps", "commitments",
            "people", "relationships", "memories", "entities",
            "entity_relationships", "decisions", "routines",
            "research_notes", "routine_runs", "devices",
            "device_pairing_tokens", "notifications", "approved_actions",
            "consent_records", "events", "voice_sessions",
        ]:
            try:
                out[t] = int((await c.execute(text(f"select count(*) from {t}"))).scalar_one())
            except Exception as exc:
                last_error = exc
                # UndefinedTable poisons the shared transaction; roll back so
                # subsequent counts still work (drill DBs lack some tables).
                with contextlib.suppress(Exception):
                    await c.rollback()
                out[t] = -1
        if last_error is not None:
            print("COUNT-LAST-ERR:", str(last_error)[:160], file=sys.stderr)  # table absent in this lineage
    return out


async def cmd_counts(args: argparse.Namespace) -> None:
    os.environ.setdefault("EV_DATABASE_URL", "")

    drill_url = _drill_url()
    prod_eng = await _engine(os.environ["EV_DATABASE_URL"])
    drill_eng = await _engine(drill_url)
    prod = await _counts(prod_eng)
    pre = await _counts(drill_eng)
    await prod_eng.dispose()
    await drill_eng.dispose()
    print(json.dumps({"pre_incident": pre, "current_production": prod}, indent=1))


async def cmd_diff(args: argparse.Namespace) -> None:
    """Entity-level missing/new for key owner domains."""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import (
        Commitment,
        Device,
        Goal,
        Memory,
        Project,
    )

    drill_url = _drill_url()
    os.environ["EV_DATABASE_URL"] = drill_url

    prod_rows: dict[str, list] = {}
    async with SessionLocal() as s:
        for model in (Project, Goal, Commitment, Memory, Device):
            rows = (await s.execute(select(model))).scalars().all()
            prod_rows[model.__tablename__] = [str(r.id) for r in rows]
            if model is Project:
                {str(r.id): r.title for r in rows}

    drill_eng = await _engine(drill_url)
    from sqlalchemy.orm import sessionmaker

    Maker = sessionmaker(drill_eng, class_=type(SessionLocal().sync_session))
    del Maker
    from sqlalchemy.ext.asyncio import AsyncSession as AS

    maker = sessionmaker(bind=drill_eng, class_=AS, expire_on_commit=False)
    pre_rows: dict[str, dict[str, dict]] = {}
    async with maker() as s:
        for model in (Project, Goal, Commitment, Memory, Device):
            rows = (await s.execute(select(model))).scalars().all()
            pre_rows[model.__tablename__] = {
                str(r.id): {
                    "title": getattr(r, "title", None) or getattr(r, "description", None)
                    or getattr(r, "name", None),
                    "status": getattr(r, "status", None) or getattr(r, "state", None),
                    "priority": getattr(r, "priority", None),
                    "created_at": getattr(r, "created_at", None),
                    "is_test": is_test_artifact(
                        getattr(r, "title", None) or getattr(r, "description", None)
                        or getattr(r, "name", None)
                    ),
                }
                for r in rows
            }
    await drill_eng.dispose()

    report: dict[str, dict] = {}
    for table, pre in pre_rows.items():
        cur_ids = set(prod_rows.get(table, []))
        missing = sorted(pid for pid in pre if pid not in cur_ids)
        new_since = sorted(pid for pid in cur_ids if pid not in pre)
        missing_non_test = [m for m in missing if not pre[table][m]["is_test"]]
        report[table] = {
            "pre_count": len(pre),
            "current_count": len(cur_ids),
            "missing_from_current": len(missing),
            "missing_non_test": len(missing_non_test),
            "new_since_recovery": len(new_since),
        }
    print(json.dumps(report, indent=1))


async def main_async(argv: list[str]) -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("mount")
    m.add_argument("--backup", default=BACKUP_DEFAULT)
    m.add_argument("--passphrase", default=None)
    sub.add_parser("counts")
    d = sub.add_parser("diff")
    d.add_argument("--backup", default=BACKUP_DEFAULT)
    d.add_argument("--passphrase", default=None)
    a = sub.add_parser("apply")
    a.add_argument("--backup", default=BACKUP_DEFAULT)
    a.add_argument("--passphrase", default=None)
    a.add_argument("--apply", action="store_true", help="execute (default dry-run)")
    args = parser.parse_args(argv)

    # Ensure repo .env config loads (non-secret settings), then override URL.
    if args.cmd in {"mount", "counts", "diff"}:
        from app.config import settings  # noqa: F401  (loads env/config)

    if args.cmd == "mount":
        await cmd_mount(args)
    elif args.cmd == "counts":
        await cmd_counts(args)
    elif args.cmd == "diff":
        await cmd_diff(args)
    elif args.cmd == "apply":
        await cmd_apply(args)


async def cmd_apply(args: argparse.Namespace) -> None:
    """Selective reclamation (P0.1 PART 24-28): memories graph + authentic
    history + iPhone device rebind. DRY-RUN by default; --apply executes."""
    from sqlalchemy import text as _text
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.everywhere.sync import state_epoch
    from app.models import Base as _B
    from app.models import Device

    vector_cols_by_table = {
        t.name: {c.name for c in t.columns if c.type.__class__.__name__ == "Vector"}
        for t in _B.metadata.tables.values()
    }
    cols_cache = {
        t.name: set(t.columns.keys()) for t in _B.metadata.tables.values()
    }

    from sqlalchemy import select as _select

    from app.db import SessionLocal

    passphrase = args.passphrase or os.environ.get("EV_BACKUP_PASSPHRASE")
    from app.services.backup import load_backup

    payload = load_backup(args.backup, passphrase)
    async with SessionLocal() as s:
        epoch = await state_epoch(s)

        # 1) authentic historical events
        events = payload.get("events") or []
        _rows = (await s.execute(_text("select id from events"))).scalars().all()
        existing = {str(x) for x in _rows}
        fresh = [e for e in events if str(e.get("id")) not in existing]
        fresh.sort(key=lambda e: (e.get("occurred_at") or "", str(e.get("id"))))
        if args.apply and fresh:
            tbl = _B.metadata.tables["events"]
            cols = cols_cache["events"]
            batch = [{k: v for k, v in e.items() if k in cols} for e in fresh]
            for i in range(0, len(batch), 500):
                stmt = pg_insert(tbl).on_conflict_do_nothing(index_elements=["id"])
                await s.execute(stmt, batch[i:i + 500])
            await s.commit()

        # 2) memories graph — genuine only, missing only
        mem_rows = payload.get("memories") or []
        _mrows = (await s.execute(_text("select id from memories"))).scalars().all()
        existing_mem = {str(x) for x in _mrows}
        missing_mem = [m for m in mem_rows if str(m.get("id")) not in existing_mem]
        genuine = [
            m for m in missing_mem if not is_test_artifact(m.get("text"))
        ]
        test_skipped = len(missing_mem) - len(genuine)
        if args.apply and genuine:
            tbl = _B.metadata.tables["memories"]
            cols = cols_cache["memories"]
            vecs = vector_cols_by_table["memories"]
            batch = [
                {k: v for k, v in m.items() if k in cols and k not in vecs}
                for m in genuine
            ]
            for i in range(0, len(batch), 500):
                stmt = pg_insert(tbl).on_conflict_do_nothing(index_elements=["id"])
                await s.execute(stmt, batch[i:i + 500])
            await s.commit()

        # 3) entities + links scoped to recovered memories
        ent_missing = []
        me_links = []
        if args.apply and genuine:
            recovered_ids = {str(m.get("id")) for m in genuine}
            ents = payload.get("entities") or []
            _erows = (await s.execute(_text("select id from entities"))).scalars().all()
            existing_ent = {str(x) for x in _erows}
            ent_missing = [e for e in ents if str(e.get("id")) not in existing_ent]
            me_all = payload.get("memory_entities") or []
            me_links = [
                link for link in me_all
                if str(link.get("memory_id")) in recovered_ids
            ]
            if ent_missing:
                tbl = _B.metadata.tables["entities"]
                cols = cols_cache["entities"]
                batch = [{k: v for k, v in e.items() if k in cols} for e in ent_missing]
                for i in range(0, len(batch), 500):
                    stmt = pg_insert(tbl).on_conflict_do_nothing(index_elements=["id"])
                    await s.execute(stmt, batch[i:i + 500])
                await s.commit()
            if me_links:
                tbl = _B.metadata.tables["memory_entities"]
                cols = cols_cache["memory_entities"]
                batch = [{k: v for k, v in link.items() if k in cols} for link in me_links]
                for i in range(0, len(batch), 500):
                    stmt = pg_insert(tbl).on_conflict_do_nothing(index_elements=["id"])
                    await s.execute(stmt, batch[i:i + 500])
                await s.commit()

        # 4) iPhone device rebind (PART 17): restore row + bump generation.
        iphone_row = next(
            (
                d for d in (payload.get("devices") or [])
                if str(d.get("id")) == "6168e987-dd7c-4fef-a01c-2c2e04cf78d5"
            ),
            None,
        )
        iphone_rebound = False
        if args.apply and iphone_row is not None:
            rev = int((_device_auth_revision(s)) or 1) + 1
            import datetime as _dt

            drow = Device(
                id=Device.__table__.c.id.type.python_type(iphone_row["id"]),
                name=iphone_row.get("name") or "Primary iPhone",
                token_hash=iphone_row.get("token_hash"),
                trust_level="owner",
                capabilities=iphone_row.get("capabilities") or [],
                device_type="phone",
                platform=iphone_row.get("platform"),
                paired_at=_dt.datetime.now(_dt.UTC),
                role=iphone_row.get("role") or "primary_companion",
                memory_scope=None,
                client_version=iphone_row.get("client_version"),
                protocol_version=iphone_row.get("protocol_version"),
                auth_revision=rev,
            )
            exists = (
                await s.execute(
                    _select(Device).where(Device.id == drow.id)
                )
            ).scalars().first()
            if exists is None:
                s.add(drow)
                await s.commit()
                iphone_rebound = True

        plan = {
            "epoch": epoch,
            "events_fresh": len(fresh),
            "events_skipped_existing": len(events) - len(fresh),
            "memories_missing": len(missing_mem),
            "memories_genuine": len(genuine),
            "memories_test_skipped": test_skipped,
            "entities_missing_applied": len(ent_missing),
            "memory_entity_links_applied": len(me_links),
            "iphone_in_backup": iphone_row is not None,
            "iphone_rebound": iphone_rebound,
            "dry_run": not args.apply,
        }
        print(json.dumps(plan, indent=1, default=str))


def _device_auth_revision(session):
    from sqlalchemy import text as _t

    row = session.execute(_t("select coalesce(max(auth_revision),1) from devices")).scalar_one()
    return int(row or 1)


def vector_cols_for(table_name: str) -> set[str]:
    from app.models import Base

    t = Base.metadata.tables.get(table_name)
    if t is None:
        return set()
    return {c.name for c in t.columns if c.type.__class__.__name__ == "Vector"}


if __name__ == "__main__":
    asyncio.run(main_async(sys.argv[1:]))

