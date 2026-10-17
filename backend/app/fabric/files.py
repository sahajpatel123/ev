"""File lane: snapshot-backed create/overwrite, Trash delete + undo, journals ok+fail."""
from __future__ import annotations

import hashlib
import shutil
import time
from pathlib import Path

from .policy import SECRET_HINTS, _receipt, journal

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
        data = content.encode() if isinstance(content, str) else content
        tp.write_bytes(data)
        # R7: verify by reading the world back — hash must match what we wrote.
        back = tp.read_bytes()
        digest = hashlib.sha256(back).hexdigest()
        ok = back == data
        undo = _snap(r, tp.relative_to(r).as_posix() if _inside(r, tp) else tp.name, prev)
        journal(r, {"lane": "files", "op": "create", "ok": ok, "target": str(path), "sha256": digest})
        if not ok:
            return _receipt(ok=False, error="read-back mismatch")
        return _receipt(ok=True, verified=True,
                        evidence={"path": str(tp), "sha256": digest, "size": len(back)},
                        undo=undo)
    except OSError as e:
        journal(r, {"lane": "files", "op": "create", "ok": False, "target": str(path)})
        return _receipt(ok=False, error=str(e))


def overwrite(path, content: str | bytes = "", root=None, **kw) -> dict:
    return create(path, content, root=root, **kw)


def delete(path, root=None, **kw) -> dict:
    r = _root(root)
    tp = (r / str(path)).resolve() if not Path(str(path)).is_absolute() else Path(str(path)).resolve()
    if any(h in tp.name.lower() for h in SECRET_HINTS):
        journal(r, {"lane": "files", "op": "delete", "ok": False, "target": str(path)})
        return _receipt(ok=False, error="secret path denied")
    if not _inside(r, tp):
        journal(r, {"lane": "files", "op": "delete", "ok": False, "target": str(path)})
        return _receipt(ok=False, error="outside roots needs confirm", confirm=True)
    try:
        if not tp.exists():
            journal(r, {"lane": "files", "op": "delete", "ok": False, "target": str(path)})
            return _receipt(ok=False, error="not found")
        tdir = r / _TRASH
        tdir.mkdir(parents=True, exist_ok=True)
        dest = tdir / (tp.name + f".{int(time.time()*1000)}")
        shutil.move(str(tp), str(dest))
        undo = {"trash": str(dest), "rel": str(path)}
        gone = not tp.exists()
        kept = dest.exists()
        journal(r, {"lane": "files", "op": "delete", "ok": gone and kept, "target": str(path)})
        if not (gone and kept):
            return _receipt(ok=False, error="trash move not verifiable")
        return _receipt(ok=True, verified=True, evidence={"trashed": str(dest)}, undo=undo)
    except OSError as e:
        journal(r, {"lane": "files", "op": "delete", "ok": False, "target": str(path)})
        return _receipt(ok=False, error=str(e))


def bulk(paths, op: str = "delete", root=None, confirm: bool = False, **kw) -> dict:
    paths = list(paths or [])
    if len(paths) > 25 and not confirm:
        journal(_root(root), {"lane": "files", "op": f"bulk-{op}", "ok": False,
                              "n": len(paths), "parked": True})
        return _receipt(ok=False, error="bulk>25 needs confirm", confirm=True)
    outs = []
    for p in paths:
        outs.append(delete(p, root=root) if op == "delete" else create(p, kw.get("content", ""), root=root))
    all_ok = all(o["ok"] for o in outs) and all(o.get("verified") for o in outs)
    journal(_root(root), {"lane": "files", "op": f"bulk-{op}", "ok": all_ok, "n": len(outs)})
    return _receipt(ok=all_ok, verified=all_ok, evidence={"n": len(outs)})


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
                restored = not target.exists()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                restored = target.read_bytes() == data
            journal(r, {"lane": "files", "op": "undo", "ok": restored, "target": undo_info["rel"]})
            return _receipt(ok=restored, verified=restored, evidence={"restored": undo_info["rel"]})
        if "trash" in undo_info:
            src = Path(undo_info["trash"])
            target = r / undo_info["rel"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(target))
            restored = target.exists() and not src.exists()
            journal(r, {"lane": "files", "op": "undo", "ok": restored, "target": undo_info["rel"]})
            return _receipt(ok=restored, verified=restored, evidence={"restored": undo_info["rel"]})
        return _receipt(ok=False, error="unknown undo")
    except OSError as e:
        return _receipt(ok=False, error=str(e))


def read_file(path, root=None, **kw) -> dict:
    r = _root(root)
    tp = (r / str(path)).resolve() if not Path(str(path)).is_absolute() else Path(str(path)).resolve()
    if any(h in tp.name.lower() for h in SECRET_HINTS):
        journal(r, {"lane": "files", "op": "read", "ok": False, "target": str(path)})
        return _receipt(ok=False, error="secret path denied")
    if not _inside(r, tp):
        journal(r, {"lane": "files", "op": "read", "ok": False, "target": str(path)})
        return _receipt(ok=False, error="outside roots needs confirm", confirm=True)
    try:
        body = tp.read_text()
        return _receipt(ok=True, verified=True, evidence={"body": body})
    except OSError as e:
        return _receipt(ok=False, error=str(e))


def list_files(root=None, **kw) -> dict:
    r = _root(root)
    try:
        names = [p.name for p in r.iterdir() if not p.name.startswith(".ev-")]
        return _receipt(ok=True, verified=True, evidence={"files": names})
    except OSError as e:
        return _receipt(ok=False, error=str(e))
