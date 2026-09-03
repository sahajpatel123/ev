"""Life-archive catalog and ingest for owner Apple/Google takeout folders.

Postgres Events remain authority. This package never copies blobs, never
reads quarantined files, and never creates a second memory taxonomy.
"""

__all__ = [
    "CatalogRecord",
    "catalog_tree",
    "classify_path",
    "ingest_records",
    "summarize",
    "write_catalog",
]


def __getattr__(name: str):
    if name == "CatalogRecord" or name == "classify_path":
        from app.memory.life_archive.classify import CatalogRecord, classify_path

        return CatalogRecord if name == "CatalogRecord" else classify_path
    if name in {"catalog_tree", "summarize", "write_catalog"}:
        from app.memory.life_archive import catalog as _catalog

        return getattr(_catalog, name)
    if name == "ingest_records":
        from app.memory.life_archive.ingest import ingest_records

        return ingest_records
    raise AttributeError(name)
