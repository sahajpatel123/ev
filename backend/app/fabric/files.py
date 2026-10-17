"""File lane: snapshot-backed create/overwrite, Trash delete + undo, journals ok+fail."""
from __future__ import annotations
import shutil
import time
from pathlib import Path
from .policy import _receipt, decide, journal, SECRET_HINTS

_TRASH = ".ev-trash"
_UNDO = ".ev-undo"


def _root(root) -> Path:
    return Path(root or ".").resolve()


def _inside(root: Path, target: str | Path) -> bool:
    try:
        Path(target).resolve().relative_to(root)
        return True
    except ValueError:
        return False


def _snap(root: Path, rel: str, data: bytes | None):
    u = root / _UNDO
    u.mkdir(parents=True, exist_ok=True)
    sp = u / (rel.replace("/", "__") + f".{int(time.time()*1000)}.snap")
    if data is None:
        sp.write_bytes(b"")
        (sp.with_suffix(sp.suffix + ".deleted")).write_text("1")
    else:
        sp.write_bytes(data)
    return {"snapshot": str(sp), "rel": rel}


def create(path, content: str | bytes = "", root=None, **kw) -> dict:
    r = _root(root)
    tp = (r / str(path)).resolve() if not Path(str(path)).is_absolute() else Path(str(path)).resolve()
    if any(h in tp.name.lower() for h in SECRET_HINTS):
        journal(r, {"lane": "files", "op": "create", "ok": False, "target": str(path)})
        return _receipt(ok=False, error="secret path denied")
    if not _inside(r, tp):
        journal(r, {"lane": "files", "op": "create", "ok": False, "target": str(path)})
        return _receipt(ok=False, error="outside roots needs confirm", confirm=True)
    try:
        existed = tp.exists()
        prev = tp.read_bytes() if existed else None
        tp.parent.mkdir(parents=True, exist_ok=True)
        tp.write_bytes(content.encode() if isinstance(content, str) else content)
        undo = _snap(r, tp.relative_to(r).as_posix() if _inside(r, tp) else tp.name, prev)
        journal(r, {"lane": "files", "op": "create", "ok": True, "target": str(path)})
        return _receipt(ok=True, evidence={"path": str(tp)}, undo=undo)
    except OSError as e:
        journal(r, {"lane": "files", "op": "create", "ok": False, "target": str(path)})
        return _receipt(ok=False, error=str(e))


def overwrite(path, content: str | bytes = "", root=None, **kw) -> dict:
    return create(path, content, root=root, **kw)


def delete(path, root=None, **kw) -> dict:
    r = _root(root)
    tp = (r / str(path)).resolve() if not Path(str(path)).is_absolute() else Path(str(path)).resolve()
    try:
        if not tp.exists():
            journal(r, {"lane": "files", "op": "delete", "ok": False, "target": str(path)})
            return _receipt(ok=False, error="not found")
        tdir = r / _TRASH
        tdir.mkdir(parents=True, exist_ok=True)
        dest = tdir / (tp.name + f".{int(time.time()*1000)}")
        shutil.move(str(tp), str(dest))
        undo = {"trash": str(dest), "rel": str(path)}
        journal(r, {"lane": "files", "op": "delete", "ok": True, "target": str(path)})
        return _receipt(ok=True, evidence={"trashed": str(dest)}, undo=undo)
    except OSError as e:
        journal(r, {"lane": "files", "op": "delete", "ok": False, "target": str(path)})
        return _receipt(ok=False, error=str(e))


def bulk(paths, op: str = "delete", root=None, **kw) -> dict:
    paths = list(paths or [])
    if len(paths) > 25:
        return _receipt(ok=False, error="bulk>25 needs confirm", confirm=True)
    outs = []
    for p in paths:
        outs.append(delete(p, root=root) if op == "delete" else create(p, kw.get("content", ""), root=root))
    return _receipt(ok=all(o["ok"] for o in outs), evidence={"n": len(outs)})


def undo(undo_info: dict, root=None) -> dict:
    try:
        r = _root(root)
        if not undo_info:
            return _receipt(ok=False, error="nothing to undo")
        if "snapshot" in undo_info:
            sp = Path(undo_info["snapshot"])
            data = sp.read_bytes()
            target = r / undo_info["rel"]
            if (sp.with_suffix(sp.suffix + ".deleted")).exists():
                if target.exists():
                    target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            return _receipt(ok=True, evidence={"restored": undo_info["rel"]})
        if "trash" in undo_info:
            src = Path(undo_info["trash"])
            target = r / undo_info["rel"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(target))
            return _receipt(ok=True, evidence={"restored": undo_info["rel"]})
        return _receipt(ok=False, error="unknown undo")
    except OSError as e:
        return _receipt(ok=False, error=str(e))


def read_file(path, root=None, **kw) -> dict:
    r = _root(root)
    tp = (r / str(path)).resolve() if not Path(str(path)).is_absolute() else Path(str(path)).resolve()
    try:
        return _receipt(ok=True, evidence={"body": tp.read_text()})
    except OSError as e:
        return _receipt(ok=False, error=str(e))


def list_files(root=None, **kw) -> dict:
    r = _root(root)
    try:
        names = [p.name for p in r.iterdir() if not p.name.startswith(".ev-")]
        return _receipt(ok=True, evidence={"files": names})
    except OSError as e:
        return _receipt(ok=False, error=str(e))
