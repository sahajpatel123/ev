"""Licensed, attributed public-figure biodata for AGENT 7 ROSTER.

Public figures resolve **by name** from Wikidata SPARQL (CC0) and Wikipedia
REST (CC BY-SA 4.0). Every field carries its source URL and license. Results
are cached with a TTL, and a public-figure record is never merged into a
private person without explicit human confirmation (``link_to_entity``).
"""

from __future__ import annotations

import asyncio
import re
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import PublicFigureCache
from app.schemas import AttributedFieldOut, PublicFigureBiodataOut
from app.utils.text import normalize_text, utcnow

WIKIDATA_CC0 = "CC0"
WIKIPEDIA_CC_BY_SA = "CC BY-SA 4.0"
COMBINED_LICENSE = "CC0 / CC BY-SA 4.0"


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalize SQLite-naive datetimes to tz-aware UTC for comparisons."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class BiodataError(Exception):
    """Domain error with an HTTP-ish status and stable error code."""

    def __init__(self, message: str, *, status: int = 503, code: str = "biodata_unavailable") -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


@dataclass
class BiodataResult:
    name: str
    occupations: list[dict]
    notable_works: list[dict]
    dates: dict | None
    summary: dict | None
    source_url: str
    license: str
    fetched_at: datetime
    cached: bool
    provider: str
    degraded: bool
    merged: bool


def _field(value: str, source_url: str, license: str) -> dict:
    return {"value": value, "source_url": source_url, "license": license}


def _date_part(value: str | None) -> str | None:
    if not value:
        return None
    return value.split("T", 1)[0]


class BiodataResolver:
    """Resolve, cache, and (only on explicit request) link public-figure biodata."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.session = session
        self._client = client

    @staticmethod
    def canonical_key(name: str) -> str:
        return f"public:{normalize_text(name)}"

    async def _http(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        return httpx.AsyncClient(timeout=settings.biodata_timeout_seconds)

    async def _sparql_json(
        self,
        client: httpx.AsyncClient,
        query: str,
    ) -> dict:
        """One bounded SPARQL GET with a single retry (Wikidata can be flaky)."""
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = await client.get(
                    settings.biodata_wikidata_sparql_url,
                    params={"query": query, "format": "json"},
                    headers={"User-Agent": "EV-ROSTER/0.1 (local personal assistant)"},
                )
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, httpx.TimeoutException) as exc:
                last_error = exc
                if attempt == 0:
                    await asyncio.sleep(0.5)
        if last_error is not None:
            raise last_error
        raise BiodataError(
            "Wikidata SPARQL request failed",
            status=503,
            code="biodata_unavailable",
        )

    async def resolve(self, name: str, *, refresh: bool = False) -> BiodataResult:
        """Resolve a public figure by name with attribution, using the TTL cache."""
        if (settings.biodata_provider or "wikidata").lower() == "none":
            raise BiodataError(
                "Public-figure biodata is disabled (EV_BIODATA_PROVIDER=none)",
                status=503,
                code="biodata_disabled",
            )
        key = self.canonical_key(name)
        row = (
            await self.session.execute(
                select(PublicFigureCache).where(PublicFigureCache.canonical_key == key)
            )
        ).scalar_one_or_none()
        now = utcnow()
        expires_at = _as_utc(row.expires_at) if row is not None else None
        if (
            row is not None
            and not refresh
            and expires_at is not None
            and expires_at > now
        ):
            data = dict(row.data)
            data.pop("cached", None)
            data.pop("merged", None)
            fetched_raw = data.pop("fetched_at", None)
            fetched_at = (
                datetime.fromisoformat(fetched_raw)
                if isinstance(fetched_raw, str)
                else now
            )
            return BiodataResult(
                **data,
                fetched_at=fetched_at,
                cached=True,
                merged=bool(row.confirmed and row.entity_id is not None),
            )

        result = await self._fetch(name)
        if row is None:
            row = PublicFigureCache(
                name=name,
                canonical_key=key,
                data={},
                source_url=result.source_url,
                license=result.license,
                fetched_at=result.fetched_at,
                expires_at=result.fetched_at
                + timedelta(seconds=settings.biodata_ttl_seconds),
                confirmed=False,
            )
            self.session.add(row)
        else:
            row.name = name
            row.source_url = result.source_url
            row.license = result.license
            row.fetched_at = result.fetched_at
            row.expires_at = result.fetched_at + timedelta(
                seconds=settings.biodata_ttl_seconds
            )
        stored = {
            "name": result.name,
            "occupations": result.occupations,
            "notable_works": result.notable_works,
            "dates": result.dates,
            "summary": result.summary,
            "source_url": result.source_url,
            "license": result.license,
            "fetched_at": result.fetched_at.isoformat(),
            "provider": result.provider,
            "degraded": result.degraded,
        }
        row.data = stored
        await self.session.flush()
        result.cached = False
        result.merged = bool(row.confirmed and row.entity_id is not None)
        return result

    async def _fetch(self, name: str) -> BiodataResult:
        client = await self._http()
        own_client = self._client is None
        now = utcnow()
        escaped_name = name.replace(chr(92), chr(92) * 2).replace(
            chr(34), chr(92) + chr(34)
        )
        try:
            qid: str | None = None
            item_label: str | None = None
            occupations: list[str] = []
            works: list[str] = []
            birth: str | None = None
            death: str | None = None
            try:
                item_sparql = (
                    "SELECT ?item ?itemLabel ?birth ?death WHERE {"
                    f' ?item rdfs:label "{escaped_name}"@en.'
                    " OPTIONAL { ?item wdt:P569 ?birth. }"
                    " OPTIONAL { ?item wdt:P570 ?death. }"
                    ' SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }'
                    " } LIMIT 5"
                )
                payload = await self._sparql_json(client, item_sparql)
                bindings = payload.get("results", {}).get("bindings", [])
                for binding in bindings:
                    if qid is None:
                        item_uri = binding.get("item", {}).get("value", "")
                        qid = item_uri.rsplit("/", 1)[-1] if item_uri else None
                    if item_label is None:
                        item_label = binding.get("itemLabel", {}).get("value")
                    if birth is None:
                        birth = _date_part(binding.get("birth", {}).get("value"))
                    if death is None:
                        death = _date_part(binding.get("death", {}).get("value"))
                occupations = await self._query_labels(
                    client, escaped_name, "wdt:P106"
                )
                works = await self._query_labels(client, escaped_name, "wdt:P800")
            except (httpx.HTTPError, httpx.TimeoutException, ValueError):
                pass  # Wikipedia fallback below still works

            title = item_label or name
            summary: dict | None = None
            page_url: str | None = None
            try:
                summary_response = await client.get(
                    f"{settings.biodata_wikipedia_rest_url.rstrip('/')}/"
                    f"{urllib.parse.quote(title)}",
                    headers={"User-Agent": "EV-ROSTER/0.1 (local personal assistant)"},
                )
                summary_response.raise_for_status()
                summary_payload = summary_response.json()
                extract = summary_payload.get("extract")
                page_url = (
                    summary_payload.get("content_urls", {})
                    .get("desktop", {})
                    .get("page")
                    or f"https://en.wikipedia.org/wiki/{urllib.parse.quote(title)}"
                )
                if extract:
                    summary = _field(
                        extract.strip()[:1000],
                        page_url,
                        WIKIPEDIA_CC_BY_SA,
                    )
            except (httpx.HTTPError, httpx.TimeoutException, ValueError):
                pass

            if qid is None and summary is None:
                raise BiodataError(
                    f"No public-figure record found for {name!r}",
                    status=404,
                    code="biodata_not_found",
                )

            source_url = (
                f"https://www.wikidata.org/wiki/{qid}" if qid else (page_url or "")
            )
            date_parts = [
                part
                for part in (
                    f"born {birth}" if birth else "",
                    f"died {death}" if death else "",
                )
                if part
            ]
            dates_value = "; ".join(date_parts)
            return BiodataResult(
                name=name,
                occupations=[
                    _field(value, source_url, WIKIDATA_CC0) for value in occupations
                ],
                notable_works=[
                    _field(value, source_url, WIKIDATA_CC0) for value in works
                ],
                dates=(
                    _field(dates_value, source_url, WIKIDATA_CC0)
                    if dates_value
                    else None
                ),
                summary=summary,
                source_url=source_url,
                license=COMBINED_LICENSE,
                fetched_at=now,
                cached=False,
                provider="wikidata",
                degraded=False,
                merged=False,
            )
        except BiodataError:
            raise
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            raise BiodataError(
                f"Public-figure biodata unavailable: {exc}",
                status=503,
                code="biodata_unavailable",
            ) from exc
        finally:
            if own_client:
                await client.aclose()

    async def _query_labels(
        self,
        client: httpx.AsyncClient,
        escaped_name: str,
        property_iri: str,
    ) -> list[str]:
        """Query one property's English labels for a named Wikidata item."""
        sparql = (
            "SELECT DISTINCT ?label WHERE {"
            f' ?item rdfs:label "{escaped_name}"@en.'
            f" ?item {property_iri} ?value."
            " ?value rdfs:label ?label."
            ' FILTER(LANG(?label) = "en")'
            " } LIMIT 50"
        )
        payload = await self._sparql_json(client, sparql)
        labels: list[str] = []
        for binding in payload.get("results", {}).get("bindings", []):
            label = binding.get("label", {}).get("value")
            if label and label not in labels and not re.fullmatch(r"Q\d+", label):
                labels.append(label)
        return labels

    async def link_to_entity(
        self,
        name: str,
        entity_id: UUID,
        *,
        actor: str,
        reason: str | None = None,
    ) -> PublicFigureCache:
        """Explicit human confirmation: merge a public figure into a private person."""
        from app.ev.edith import record_command

        key = self.canonical_key(name)
        row = (
            await self.session.execute(
                select(PublicFigureCache).where(PublicFigureCache.canonical_key == key)
            )
        ).scalar_one_or_none()
        if row is None:
            await self.resolve(name)
            row = (
                await self.session.execute(
                    select(PublicFigureCache).where(
                        PublicFigureCache.canonical_key == key
                    )
                )
            ).scalar_one()
        row.entity_id = entity_id
        row.confirmed = True
        row.updated_at = utcnow()
        await record_command(
            self.session,
            command_type="people.biodata.link",
            actor=actor,
            target_type="person",
            target_id=str(entity_id),
            request={"name": name, "reason": reason},
            result={"confirmed": True},
            status="completed",
        )
        await self.session.flush()
        return row

    async def delete_for_entity(self, entity_id: UUID) -> int:
        ids = list(
            (
                await self.session.execute(
                    select(PublicFigureCache.id).where(
                        PublicFigureCache.entity_id == entity_id
                    )
                )
            )
            .scalars()
            .all()
        )
        if ids:
            await self.session.execute(
                delete(PublicFigureCache).where(PublicFigureCache.id.in_(ids))
            )
        await self.session.flush()
        return len(ids)

    async def to_schema(self, result: BiodataResult) -> PublicFigureBiodataOut:
        def to_out(field: dict | None) -> AttributedFieldOut | None:
            if field is None:
                return None
            return AttributedFieldOut(**field)

        return PublicFigureBiodataOut(
            name=result.name,
            occupations=[
                AttributedFieldOut(**field) for field in result.occupations
            ],
            notable_works=[
                AttributedFieldOut(**field) for field in result.notable_works
            ],
            dates=to_out(result.dates),
            summary=to_out(result.summary),
            source_url=result.source_url,
            license=result.license,
            fetched_at=result.fetched_at,
            cached=result.cached,
            provider=result.provider,
            degraded=result.degraded,
            merged=result.merged,
        )
