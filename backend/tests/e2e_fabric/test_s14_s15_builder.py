"""S14/S15: builder — real plan execution with test summary; kill mid-way and resume."""



def test_s14_build_plan_and_apply(tmp_path):
    import sys

    from app.fabric.builder import apply_plan, build_plan, resume_job
    proj = tmp_path / "todoapp"
    proj.mkdir()
    (proj / "todo.py").write_text("def add(a, b):\n    return a + b\n")
    (proj / "test_todo.py").write_text(
        "from todo import add\ndef test_add():\n    assert add(1, 2) == 3\n")
    steps = [
        {"name": "run tests", "cmd": f"{sys.executable} -m pytest test_todo.py -q",
         "cwd": str(proj)},
    ]
    p = build_plan("build todo app", root=tmp_path, project_dir=str(proj), steps=steps)
    assert p["ok"] and p["verified"], p
    assert p["plan"]["project_dir"] == str(proj)
    r = apply_plan(p, root=tmp_path)
    assert r["ok"] and r["verified"], r
    assert "run tests: ok" in r["evidence"]["summary"], r
    assert "1 passed" in r["evidence"]["tail"], r
    done = resume_job(p["job_id"], root=tmp_path)
    assert done["evidence"]["state"] == "done", done


def test_s15_kill_and_resume(tmp_path):
    from app.fabric.builder import apply_plan, build_plan, resume_job
    steps = [
        {"name": "step-one", "cmd": "echo one"},
        {"name": "step-two", "cmd": "echo two"},
        {"name": "step-three", "cmd": "echo three"},
    ]
    p = build_plan("killable job", root=tmp_path, steps=steps)
    killed = apply_plan(p, root=tmp_path, stop_after=1)
    assert not killed["ok"], killed
    assert killed["evidence"]["state"] == "killed", killed
    assert "phantom" not in str(killed).lower()
    st = resume_job(p["job_id"], root=tmp_path)
    assert st["ok"] and st["verified"], st
    assert st["evidence"]["state"] == "killed", st
    assert st["evidence"]["steps_done"] == 1, st
    assert resume_job("job-nope", root=tmp_path)["error"] == "unknown job"
