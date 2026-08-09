"""Maker companion: project state machine, BOM inventory, and print queue."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BomItem, MakerProject, PrintJob
from app.schemas import (
    BomItemCreate,
    MakerProjectCreate,
    MakerProjectStatusUpdate,
    PrintJobCreate,
    PrintJobStatusUpdate,
)
from app.utils.text import utcnow

PROJECT_FLOW = ["idea", "planning", "sourcing", "building", "testing", "done"]
VALID_STATUSES = set(PROJECT_FLOW) | {"paused"}


async def create_project(session: AsyncSession, data: MakerProjectCreate) -> MakerProject:
    project = MakerProject(
        name=data.name,
        description=data.description,
        status=data.status,
        current_step=data.current_step,
    )
    session.add(project)
    await session.flush()
    return project


async def list_projects(session: AsyncSession, *, status: str | None = None, limit: int = 50) -> list[MakerProject]:
    stmt = select(MakerProject).order_by(MakerProject.updated_at.desc()).limit(min(limit, 200))
    if status:
        stmt = stmt.where(MakerProject.status == status)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_project(session: AsyncSession, project_id: UUID) -> MakerProject | None:
    return await session.get(MakerProject, project_id)


async def find_project_by_name(session: AsyncSession, name: str) -> MakerProject | None:
    result = await session.execute(
        select(MakerProject).where(MakerProject.name.ilike(f"%{name}%")).limit(1)
    )
    return result.scalars().first()


def _transition(current: str, target: str) -> bool:
    if current == target:
        return True
    if current == "paused":
        return target in VALID_STATUSES and target != "paused"
    if target == "paused":
        return True
    if current == "done":
        return False
    return PROJECT_FLOW.index(target) == PROJECT_FLOW.index(current) + 1


async def update_status(
    session: AsyncSession,
    project_id: UUID,
    data: MakerProjectStatusUpdate,
) -> MakerProject:
    project = await get_project(session, project_id)
    if project is None:
        raise KeyError(f"Project {project_id} not found")
    if not _transition(project.status, data.status):
        raise ValueError(f"Invalid transition {project.status} -> {data.status}")
    project.status = data.status
    if data.current_step is not None:
        project.current_step = data.current_step
    return project


def next_step(project: MakerProject) -> dict:
    if project.status == "paused":
        return {
            "project_id": project.id,
            "name": project.name,
            "current_status": project.status,
            "next_status": "planning" if project.current_step in (None, "idea") else project.current_step,
            "next_step": "Resume the project: restore context from memory, then continue the current step.",
        }
    if project.status == "done":
        return {
            "project_id": project.id,
            "name": project.name,
            "current_status": project.status,
            "next_status": "done",
            "next_step": "Project complete. Record the outcome and lessons as a decision follow-up.",
        }
    idx = PROJECT_FLOW.index(project.status)
    next_status = PROJECT_FLOW[idx + 1] if idx + 1 < len(PROJECT_FLOW) else "done"
    steps = {
        "idea": "Define the acceptance criteria and one-page spec.",
        "planning": "Finalize the BOM and source materials; check reorder thresholds.",
        "sourcing": "Order/collect materials and start the first build step.",
        "building": "Build the prototype and log each step to memory.",
        "testing": "Test against acceptance criteria; record results.",
        "done": "Close out and record lessons.",
    }
    return {
        "project_id": project.id,
        "name": project.name,
        "current_status": project.status,
        "next_status": next_status,
        "next_step": steps[next_status],
    }


async def add_bom_item(session: AsyncSession, project_id: UUID, data: BomItemCreate) -> BomItem:
    project = await get_project(session, project_id)
    if project is None:
        raise KeyError(f"Project {project_id} not found")
    item = BomItem(
        project_id=project_id,
        name=data.name,
        qty=data.qty,
        unit=data.unit,
        location=data.location,
        reorder_at=data.reorder_at,
        cost=data.cost,
    )
    session.add(item)
    await session.flush()
    return item


async def list_bom(session: AsyncSession, project_id: UUID) -> list[BomItem]:
    result = await session.execute(
        select(BomItem).where(BomItem.project_id == project_id).order_by(BomItem.created_at.asc())
    )
    return list(result.scalars().all())


async def delete_bom_item(session: AsyncSession, item_id: UUID) -> None:
    item = await session.get(BomItem, item_id)
    if item is None:
        raise KeyError(f"BOM item {item_id} not found")
    await session.delete(item)


async def reorder_items(session: AsyncSession, *, window_days: int = 7) -> list[dict]:
    """BOM items at/below reorder threshold, for proactive alerts."""
    rows = list(
        (
            await session.execute(
                select(BomItem).where(BomItem.reorder_at.is_not(None))
            )
        ).scalars().all()
    )
    return [
        {
            "item_id": str(item.id),
            "project_id": str(item.project_id),
            "name": item.name,
            "qty": item.qty,
            "reorder_at": item.reorder_at,
            "unit": item.unit,
            "location": item.location,
        }
        for item in rows
        if item.qty <= (item.reorder_at or 0)
    ]


async def create_print_job(session: AsyncSession, project_id: UUID, data: PrintJobCreate) -> PrintJob:
    project = await get_project(session, project_id)
    if project is None:
        raise KeyError(f"Project {project_id} not found")
    job = PrintJob(
        project_id=project_id,
        name=data.name,
        status="queued",
        estimated_minutes=data.estimated_minutes,
        filament_grams=data.filament_grams,
    )
    session.add(job)
    await session.flush()
    return job


async def list_print_jobs(session: AsyncSession, project_id: UUID) -> list[PrintJob]:
    result = await session.execute(
        select(PrintJob).where(PrintJob.project_id == project_id).order_by(PrintJob.created_at.desc())
    )
    return list(result.scalars().all())


async def update_print_job(
    session: AsyncSession,
    job_id: UUID,
    data: PrintJobStatusUpdate,
) -> PrintJob:
    job = await session.get(PrintJob, job_id)
    if job is None:
        raise KeyError(f"Print job {job_id} not found")
    job.status = data.status
    if data.error_log is not None:
        job.error_log = data.error_log
    if data.status in ("done", "failed"):
        job.finished_at = utcnow()
    return job
