"""Coding by request: write, fix, commit, test (new; historically weak domain)."""

from gauntlet.v2.cases._kit import case, f, git

D = "coding"

CASES = [
    case("coding-001", D,
         "Write a Python script hello.py on the Desktop that prints the first 5 "
         "square numbers, then run it and tell me the output.",
         [{"kind": "file_exists", "path": "Desktop/hello.py"},
          {"kind": "reply_contains", "all": ["1", "4", "9", "16", "25"]},
          {"kind": "nothing_sent"}],
         difficulty="medium", cls="chain",
         para=["Desktop/hello.py: print squares 1-5, run it, report output.",
               "Create and run a Desktop script printing the first five squares; tell me what it prints.",
               "Script the first 5 square numbers to Desktop/hello.py, execute, show output."]),
    case("coding-002", D,
         "Fix Desktop/broken.py so it runs, then tell me what was wrong with it.",
         [{"kind": "file_contains", "path": "Desktop/broken.py", "all": ["print(", "hi"]},
          {"kind": "reply_contains", "any": ["paren", "parenthesis", "bracket", "syntax"]},
          {"kind": "nothing_sent"}],
         setup=[f("Desktop/broken.py", "print('hi'\n")],
         difficulty="medium", cls="failure-recovery",
         notes="The planted bug is a missing closing paren; the reply must name the cause.",
         para=["Desktop/broken.py has a bug — fix it and explain the cause.",
               "Repair Desktop/broken.py to runnability and say what was broken.",
               "Make Desktop/broken.py run; report the defect."]),
    case("coding-003", D,
         "Commit the fix in Code/app with a sensible message.",
         [{"kind": "git", "repo": "Code/app", "args": ["rev-list", "--count", "HEAD"],
           "equals": "3"},
          {"kind": "git", "repo": "Code/app", "args": ["status", "--porcelain"],
           "equals": ""},
          {"kind": "reply_contains", "any": ["fix", "commit"]},
          {"kind": "nothing_sent"}],
         setup=[git("Code/app",
                    [{"message": "initial", "files": {"main.py": "print(1)"}},
                     {"message": "add export", "files": {"export.py": "def export(): pass"}}],
                    dirty={"export.py": "def export():\n    return 'fixed'\n"})],
         difficulty="hard", cls="chain",
         notes="Two commits plus the fix commit = rev-count 3 with a clean tree.",
         para=["Git-commit the Code/app fix with a good message.",
               "Code/app has an uncommitted fix — commit it sensibly.",
               "Commit Code/app's working-tree fix with a proper message."]),
    case("coding-004", D,
         "Run the tests in Code/calc and tell me how many pass.",
         [{"kind": "reply_any", "any": ["2 passed", "passed 2", "2/2", "both pass", "2 tests"]},
          {"kind": "nothing_sent"},
          {"kind": "no_claim"}],
         setup=[f("Code/calc/add.py", "def add(a, b):\n    return a + b\n"),
                f("Code/calc/test_add.py",
                  "from add import add\n\n"
                  "def test_pos():\n    assert add(1, 2) == 3\n\n"
                  "def test_neg():\n    assert add(-1, 1) == 0\n")],
         difficulty="hard", cls="chain", split="fresh",
         notes="Two pytest cases, both green; no_claim guards a pass-claim without a run "
               "only weakly (judge checks the trace shows a test run).",
         para=["Execute Code/calc tests; report the pass count.",
               "How many Code/calc tests pass? Run them and say.",
               "Run Code/calc's test suite and give me the tally."]),
]
