"""File lane: snapshot-backed create/overwrite, Trash delete + undo, journals ok+fail.

Containment is defense in depth, checked on the RESOLVED path at use time:
deny-listed names/parts/suffixes (fabric hints + laptop DENY sets + Library
internals) are hard DENY; outside-root and hardlinks park for confirmation
(bypassable only with an explicit owner approval); FIFOs/sockets/devices are
hard DENY (they cannot be executed safely); reads/writes go through a
stat-open-fstat identity guard so a swap mid-call fails closed instead of
touching the wrong file. Symlinks themselves are never followed on delete.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import shutil
import stat as _stat
import time
from pathlib import Path

from app.ev.laptop_files import DENY_NAMES, DENY_PARTS, DENY_SUBSTRINGS, DENY_SUFFIXES

from .policy import SECRET_HINTS, _receipt, journal

_TRASH = ".ev-trash"
_UNDO = ".ev-undo"

_LOWER_NAMES = frozenset(n.lower() for n in DENY_NAMES)
_LOWER_PARTS = frozenset(p.lower() for p in DENY_PARTS)


def _root(root) -> Path:
    return Path(root or ".").resolve()


def _target_path(root: Path, path: str | Path) -> Path:
    s = str(path)
    return (root / s).resolve() if not Path(s).is_absolute() else Path(s).resolve()


def _inside(root: Path, target: str | Path) -> bool:
    try:
        Path(target).resolve().relative_to(root)
        return True
    except ValueError:
        return False


def _malformed(path_str: str) -> bool:
    if not path_str or "\x00" in path_str:
        return True
    return any(len(part.encode("utf-8", "ignore")) > 255 for part in Path(path_str).parts)


def _in_library(resolved: Path) -> bool:
    try:
        home = Path.home().resolve()
    except OSError:
        return False
    library = home / "Library"
    icloud = library / "Mobile Documents"
    try:
        resolved.relative_to(library)
    except ValueError:
        return False
    try:
        resolved.relative_to(icloud)
        return False
    except ValueError:
        return True


def _secret_reason(resolved: Path) -> str | None:
    name = resolved.name.lower()
    if any(h in name for h in SECRET_HINTS):
        return "secret path denied"
    if name in _LOWER_NAMES:
        return "secret path denied"
    if any(part.lower() in _LOWER_PARTS for part in resolved.parts):
        return "secret path denied"
    if any(name.endswith(suffix.lower()) for suffix in DENY_SUFFIXES):
        return "secret path denied"
    lowered = str(resolved).replace("\\", "/").lower()
    if any(token in lowered for token in DENY_SUBSTRINGS):
        return "secret path denied"
    if _in_library(resolved):
        return "secret path denied"
    return None


def _target_flag(tp: Path) -> str | None:
    """None when safe; 'special' for non-regular files; 'hardlink' for nlink>1."""
    try:
        resolved = tp.resolve()
    except OSError:
        return "special"
    try:
        st = resolved.stat()
    except FileNotFoundError:
        return None  # creatable
    except OSError:
        return "special"
    if not _stat.S_ISREG(st.st_mode) and not _stat.S_ISDIR(st.st_mode):
        return "special"
    if _stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
        return "hardlink"
    return None


def _is_safe_target(tp: Path) -> bool:
    return _target_flag(tp) is None


def _identity(tp: Path) -> tuple[int, int] | None:
    try:
        st = tp.stat()
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


# iCloud File Provider materialization, AV scans, and Spotlight indexing can
# fail an open/read with a transient errno (or swap the inode mid-call).
# One retry turns those races into reads; anything persistent still fails
# honestly with the original error.
_TRANSIENT_READ_ERRNOS = frozenset({errno.EDEADLK, errno.EAGAIN, errno.EBUSY})


def _guarded_read_bytes(tp: Path, *, _retried: bool = False) -> bytes:
    """Read with stat-open-fstat identity: a mid-call swap fails, never leaks."""
    before = _identity(tp)
    if before is None:
        raise FileNotFoundError(str(tp))
    try:
        fd = os.open(tp, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno in _TRANSIENT_READ_ERRNOS and not _retried:
            time.sleep(0.5)
            return _guarded_read_bytes(tp, _retried=True)
        raise
    try:
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) != before:
            if not _retried:
                time.sleep(0.3)
                return _guarded_read_bytes(tp, _retried=True)
            raise OSError("file changed during read")
        with os.fdopen(os.dup(fd), "rb") as fh:
            return fh.read()
    except OSError as exc:
        if exc.errno in _TRANSIENT_READ_ERRNOS and not _retried:
            time.sleep(0.5)
            return _guarded_read_bytes(tp, _retried=True)
        raise
    finally:
        os.close(fd)


def _guarded_write_bytes(tp: Path, data: bytes) -> None:
    """Write with symlink + identity guards (create-new and overwrite)."""
    before = _identity(tp)
    if before is None:
        fd = os.open(tp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    else:
        fd = os.open(tp, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
    try:
        if before is not None:
            st = os.fstat(fd)
            if (st.st_dev, st.st_ino) != before:
                raise OSError("file changed during write")
        with os.fdopen(os.dup(fd), "wb") as fh:
            fh.write(data)
    finally:
        os.close(fd)


def _gate(root: Path, tp: Path, path_str: str, *, confirm: bool,
          provenance: str = "owner_turn") -> dict | None:
    """Containment gate. Returns a denial/park receipt, or None to proceed."""
    from .policy import UNTRUSTED_PROVENANCE

    secret = _secret_reason(tp)
    if secret is not None:
        return _receipt(ok=False, error=secret)
    flag = _target_flag(tp)
    if flag == "special":
        return _receipt(ok=False, error="unsupported file type")
    # R8: untrusted-triggered actions escalate to CONFIRM regardless of tier.
    if not confirm and str(provenance or "").lower() in UNTRUSTED_PROVENANCE:
        return _receipt(ok=False, error="untrusted provenance needs confirm",
                        confirm=True)
    if not _inside(root, tp):
        if confirm:
            return None
        return _receipt(ok=False, error="outside roots needs confirm", confirm=True)
    if flag == "hardlink" and not confirm:
        return _receipt(ok=False, error="hardlink needs confirm", confirm=True)
    return None


def _snap(root: Path, rel: str, data: bytes | None):
    import json as _json

    u = root / _UNDO
    u.mkdir(parents=True, exist_ok=True)
    sp = u / (rel.replace("/", "__") + f".{int(time.time()*1000)}.snap")
    if data is None:
        sp.write_bytes(b"")
        (sp.with_suffix(sp.suffix + ".deleted")).write_text("1")
    else:
        sp.write_bytes(data)
    # Sidecar so the undo API can restore by id after a restart.
    with contextlib.suppress(OSError):
        (u / (sp.name + ".info.json")).write_text(_json.dumps({"rel": rel}))
    return {"snapshot": str(sp), "rel": rel}


def create(path, content: str | bytes = "", root=None, **kw) -> dict:
    r = _root(root)
    if _malformed(str(path)):
        return _receipt(ok=False, error="bad path")
    tp = _target_path(r, path)
    gated = _gate(r, tp, str(path), confirm=bool(kw.get("confirm")),
                  provenance=str(kw.get("provenance") or "owner_turn"))
    if gated is not None:
        journal(r, {"lane": "files", "op": "create", "ok": False, "target": str(path)})
        return gated
    try:
        existed = tp.exists()
        prev = _guarded_read_bytes(tp) if existed else None
        tp.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode() if isinstance(content, str) else content
        _guarded_write_bytes(tp, data)
        # R7: verify by reading the world back — hash must match what we wrote.
        back = _guarded_read_bytes(tp)
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
    if _malformed(str(path)):
        return _receipt(ok=False, error="bad path")
    raw = Path(str(path))
    candidate = (r / raw) if not raw.is_absolute() else raw
    # A symlink itself is unlinked, never followed: deleting a link must not
    # delete its target.
    try:
        is_link = candidate.is_symlink()
    except OSError:
        is_link = False
    if is_link:
        # Lexical containment of the LINK itself (unlink never follows, so
        # resolution would wrongly gate links pointing outside).
        try:
            Path(os.path.abspath(candidate)).relative_to(r)
            lexical_inside = True
        except ValueError:
            lexical_inside = False
        if not lexical_inside:
            journal(r, {"lane": "files", "op": "delete", "ok": False, "target": str(path)})
            return _receipt(ok=False, error="outside roots needs confirm", confirm=True)
        try:
            target = os.readlink(candidate)
            candidate.unlink()
            gone = not candidate.exists()
        except OSError as e:
            journal(r, {"lane": "files", "op": "delete", "ok": False, "target": str(path)})
            return _receipt(ok=False, error=str(e))
        journal(r, {"lane": "files", "op": "delete-link", "ok": gone, "target": str(path)})
        if not gone:
            return _receipt(ok=False, error="link not removed")
        return _receipt(ok=True, verified=True, evidence={"unlinked": str(path)},
                        undo={"relink": str(candidate), "target": target})
    tp = _target_path(r, path)
    gated = _gate(r, tp, str(path), confirm=bool(kw.get("confirm")),
                  provenance=str(kw.get("provenance") or "owner_turn"))
    if gated is not None:
        journal(r, {"lane": "files", "op": "delete", "ok": False, "target": str(path)})
        return gated
    try:
        if not tp.exists():
            journal(r, {"lane": "files", "op": "delete", "ok": False, "target": str(path)})
            return _receipt(ok=False, error="not found")
        tdir = r / _TRASH
        tdir.mkdir(parents=True, exist_ok=True)
        dest = tdir / (tp.name + f".{int(time.time()*1000)}")
        shutil.move(str(tp), str(dest))
        with contextlib.suppress(OSError):
            import json as _json

            (tdir / (dest.name + ".info.json")).write_text(_json.dumps({"rel": str(path)}))
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
        if op == "delete":
            outs.append(delete(p, root=root, confirm=confirm,
                               provenance=kw.get("provenance", "owner_turn")))
        else:
            outs.append(create(p, kw.get("content", ""), root=root, confirm=confirm,
                               provenance=kw.get("provenance", "owner_turn")))
    all_ok = all(o["ok"] for o in outs) and all(o.get("verified") for o in outs)
    journal(_root(root), {"lane": "files", "op": f"bulk-{op}", "ok": all_ok, "n": len(outs)})
    return _receipt(ok=all_ok, verified=all_ok, evidence={"n": len(outs)})


def _undo_inside(root: Path, rel: str) -> Path | None:
    if _malformed(rel) or ".." in Path(rel).parts:
        return None
    target = (root / rel).resolve()
    if not _inside(root, target):
        return None
    return target


def undo(undo_info: dict, root=None) -> dict:
    try:
        r = _root(root)
        if not undo_info:
            return _receipt(ok=False, error="nothing to undo")
        if "relink" in undo_info:
            link = Path(str(undo_info["relink"]))
            if not _inside(r, link) or link.exists():
                return _receipt(ok=False, error="relink refused")
            link.symlink_to(str(undo_info.get("target") or ""))
            restored = link.is_symlink()
            journal(r, {"lane": "files", "op": "undo", "ok": restored,
                        "target": str(undo_info.get("relink"))})
            return _receipt(ok=restored, verified=restored,
                            evidence={"restored": str(undo_info.get("relink"))})
        if "snapshot" in undo_info:
            sp = Path(str(undo_info["snapshot"]))
            try:
                sp.resolve().relative_to((r / _UNDO).resolve())
            except (OSError, ValueError):
                return _receipt(ok=False, error="snapshot outside undo store")
            target = _undo_inside(r, str(undo_info.get("rel") or ""))
            if target is None:
                return _receipt(ok=False, error="undo target outside roots")
            data = _guarded_read_bytes(sp)
            if (sp.parent / (sp.name + ".deleted")).exists() or (
                    sp.with_suffix(sp.suffix + ".deleted")).exists():
                if target.exists():
                    target.unlink()
                restored = not target.exists()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                _guarded_write_bytes(target, data)
                restored = _guarded_read_bytes(target) == data
            journal(r, {"lane": "files", "op": "undo", "ok": restored,
                        "target": str(undo_info.get("rel"))})
            return _receipt(ok=restored, verified=restored,
                            evidence={"restored": str(undo_info.get("rel"))})
        if "trash" in undo_info:
            src = Path(str(undo_info["trash"]))
            try:
                src.resolve().relative_to((r / _TRASH).resolve())
            except (OSError, ValueError):
                return _receipt(ok=False, error="trash entry outside trash store")
            target = _undo_inside(r, str(undo_info.get("rel") or ""))
            if target is None:
                return _receipt(ok=False, error="undo target outside roots")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(target))
            restored = target.exists() and not src.exists()
            journal(r, {"lane": "files", "op": "undo", "ok": restored,
                        "target": str(undo_info.get("rel"))})
            return _receipt(ok=restored, verified=restored,
                            evidence={"restored": str(undo_info.get("rel"))})
        return _receipt(ok=False, error="unknown undo")
    except OSError as e:
        return _receipt(ok=False, error=str(e))


def read_file(path, root=None, **kw) -> dict:
    r = _root(root)
    if _malformed(str(path)):
        return _receipt(ok=False, error="bad path")
    tp = _target_path(r, path)
    gated = _gate(r, tp, str(path), confirm=bool(kw.get("confirm")),
                  provenance=str(kw.get("provenance") or "owner_turn"))
    if gated is not None:
        journal(r, {"lane": "files", "op": "read", "ok": False, "target": str(path)})
        return gated
    try:
        body = _guarded_read_bytes(tp).decode("utf-8", "replace")
        return _receipt(ok=True, verified=True, evidence={"body": body})
    except FileNotFoundError:
        journal(r, {"lane": "files", "op": "read", "ok": False, "target": str(path)})
        return _receipt(ok=False, error="not found")
    except OSError as e:
        journal(r, {"lane": "files", "op": "read", "ok": False, "target": str(path)})
        return _receipt(ok=False, error=str(e))


def list_files(root=None, **kw) -> dict:
    r = _root(root)
    try:
        names = [p.name for p in r.iterdir() if not p.name.startswith(".ev-")]
        return _receipt(ok=True, verified=True, evidence={"files": names})
    except OSError as e:
        return _receipt(ok=False, error=str(e))
