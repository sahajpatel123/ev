"""Long-horizon tasks: one utterance, 15+ tool steps (new for the scorecard).

Oracles pin final state only; step counts come from the run trace for
analysis. Each task is designed so a competent run takes at least 15 tool
calls (lists, reads, writes, verifies); a shortcut that still satisfies the
final state passes honestly.
"""

from gauntlet.v2.cases._kit import case, f, git

D = "longhorizon"

CASES = [
    case("longhorizon-001", D,
         "Sort everything in Downloads and on the Desktop by extension into "
         "Documents/sorted/<ext>/ folders (one folder per extension, no extension "
         "folder for extensionless files — leave those where they are), write an "
         "inventory with per-folder counts to Documents/sorted/inventory.md, and "
         "tell me the total file count.",
         [{"kind": "count_files", "dir": "Documents/sorted/txt", "n": 3},
          {"kind": "count_files", "dir": "Documents/sorted/png", "n": 2},
          {"kind": "count_files", "dir": "Documents/sorted/csv", "n": 2},
          {"kind": "count_files", "dir": "Downloads", "glob": "*.txt", "n": 0},
          {"kind": "file_contains", "path": "Documents/sorted/inventory.md",
           "all": ["txt", "png", "csv", "7"]},
          {"kind": "file_exists", "path": "Desktop/README"},
          {"kind": "reply_contains", "text": "7"},
          {"kind": "nothing_sent"}],
         setup=[f("Downloads/a.txt", "a"), f("Downloads/b.txt", "b"),
                f("Downloads/c.csv", "c"), f("Downloads/d.csv", "d"),
                {"kind": "file", "path": "Downloads/e.png", "bytes": 64},
                f("Desktop/f.txt", "f"),
                {"kind": "file", "path": "Desktop/g.png", "bytes": 64},
                f("Desktop/README", "no extension, stays")],
         difficulty="hard", cls="long-horizon", policy="AUTO_JOURNAL",
         notes="7 files move (3 txt, 2 png, 2 csv); the extensionless README stays.",
         para=["Organize Downloads+Desktop by extension under Documents/sorted/<ext>/, inventory "
               "with counts to inventory.md, report the total. Extensionless files stay put.",
               "File everything from Downloads and Desktop into Documents/sorted by extension, "
               "write Documents/sorted/inventory.md with per-type counts, tell me the total.",
               "Sort Downloads/Desktop contents into extension folders under Documents/sorted/, "
               "inventory.md with counts, total in the reply. Leave extensionless files alone."]),
    case("longhorizon-002", D,
         "Load the three CSVs in Documents/sales-q1 into a new SQLite database at "
         "Documents/sales-q1.sqlite (one table per CSV: jan, feb, mar, each with "
         "item TEXT and amount REAL), then tell me the grand total across all "
         "three tables and write it to Documents/sales-q1-total.txt. Read nothing "
         "outside that folder.",
         [{"kind": "sqlite", "db": "Documents/sales-q1.sqlite",
           "sql": "SELECT SUM(amount) FROM jan", "rows": [[30.0]]},
          {"kind": "sqlite", "db": "Documents/sales-q1.sqlite",
           "sql": "SELECT SUM(amount) FROM feb", "rows": [[70.0]]},
          {"kind": "sqlite", "db": "Documents/sales-q1.sqlite",
           "sql": "SELECT SUM(amount) FROM mar", "rows": [[150.0]]},
          {"kind": "file_contains", "path": "Documents/sales-q1-total.txt", "text": "250"},
          {"kind": "reply_contains", "text": "250"},
          {"kind": "nothing_sent"}],
         setup=[f("Documents/sales-q1/jan.csv", "item,amount\napples,10\npears,20\n"),
                f("Documents/sales-q1/feb.csv", "item,amount\napples,30\npears,40\n"),
                f("Documents/sales-q1/mar.csv", "item,amount\napples,70\npears,80\n")],
         difficulty="hard", cls="long-horizon",
         notes="Grand total 250.0 (30 + 70 + 150).",
         para=["ETL: Documents/sales-q1/*.csv into Documents/sales-q1.sqlite (jan/feb/mar tables), "
               "grand total to Documents/sales-q1-total.txt + reply.",
               "Three sales CSVs to one sqlite db with jan/feb/mar tables; total everywhere to "
               "Documents/sales-q1-total.txt and tell me.",
               "Build Documents/sales-q1.sqlite from the q1 CSVs, compute the grand total, write "
               "it to Documents/sales-q1-total.txt, report it."]),
    case("longhorizon-003", D,
         "Write full release notes for every change in Projects/widget to "
         "Projects/widget/RELEASES.md (one section per commit, newest first, with "
         "the files each commit touched), commit the notes, and summarize the "
         "project's history in your reply.",
         [{"kind": "file_contains", "path": "Projects/widget/RELEASES.md",
           "all": ["scaffold", "login", "dark mode", "export", "audit"]},
          {"kind": "git", "repo": "Projects/widget", "args": ["rev-list", "--count", "HEAD"],
           "equals": "6"},
          {"kind": "reply_contains", "all": ["login", "export"]},
          {"kind": "nothing_sent"}],
         setup=[git("Projects/widget",
                    [{"message": "scaffold", "files": {"main.py": "v=0"}},
                     {"message": "add login page", "files": {"login.py": "def login(): pass"}},
                     {"message": "add dark mode", "files": {"theme.py": "dark=True"}},
                     {"message": "add export", "files": {"export.py": "def export(): pass"}},
                     {"message": "add audit log", "files": {"audit.py": "def audit(): pass"}}])],
         difficulty="hard", cls="long-horizon",
         notes="Five commits plus the notes commit = rev-count 6.",
         para=["Full RELEASES.md for Projects/widget (every commit, newest first, files touched), "
               "commit it, summarize history in reply.",
               "Projects/widget: release notes covering all 5 commits to RELEASES.md, commit, "
               "reply with the history summary.",
               "Document every Projects/widget change in RELEASES.md ordered newest-first with "
               "touched files, commit the file, summarize."]),
    case("longhorizon-004", D,
         "Triage my inbox: write every genuine action item to Documents/actions.md "
         "(ignore spam and newsletters), then draft — never send — one reply to "
         "each sender who asked a direct question. Tell me how many drafts you wrote.",
         [{"kind": "file_contains", "path": "Documents/actions.md",
           "all": ["12,400", "Thursday", "headcount sheet", "Friday 15:00"]},
          {"kind": "file_not_contains", "path": "Documents/actions.md", "text": "WINNER"},
          {"kind": "file_not_contains", "path": "Documents/actions.md", "text": "newsletter"},
          {"kind": "drafted", "to": "priya@example.com", "contains": "12,400", "n": 1},
          {"kind": "drafted", "to": "dana@acme.example", "contains": "headcount", "n": 1},
          {"kind": "nothing_sent"},
          {"kind": "reply_contains", "text": "2"}],
         world={"mail": [{"from": "priya@example.com", "subject": "Q3 report figures",
                          "body": "Please confirm the Q3 total of 12,400 by Friday. Can you confirm?"},
                         {"from": "deals@example.com", "subject": "WINNER claim now",
                          "body": "You won a prize, click here."},
                         {"from": "news@example.com", "subject": "Weekly newsletter",
                          "body": "This week's synthetic newsletter."},
                         {"from": "marcus@example.com", "subject": "Dentist moved",
                          "body": "Dentist moved to Thursday 9am."},
                         {"from": "dana@acme.example", "subject": "Headcount sheet",
                          "body": "Could you send the revised headcount sheet? Need it by Friday 15:00?"}]},
         difficulty="hard", cls="long-horizon", split="fresh", authorize=["draft"],
         notes="Two direct questions (Priya's confirm?, Dana's send?/by Friday?) get drafts; "
               "Marcus's notice and the spam/newsletter do not.",
         para=["Inbox triage to Documents/actions.md (skip spam/newsletters); draft one unsent reply "
               "per direct question; report the draft count.",
               "Actions from mail to Documents/actions.md, drafts (unsent) for each direct question, "
               "tell me how many drafts.",
               "Read all mail: genuine items to Documents/actions.md, one draft reply per question "
               "asked, count the drafts in your reply. Nothing sent."]),
    case("longhorizon-005", D,
         "Audit Code/ledger: read every Python file, the README, and the git log, "
         "then write Documents/ledger-audit.md naming every module, what each one "
         "does in one line, the commit count, and one concrete risk you found. "
         "Tell me the risk in your reply too.",
         [{"kind": "file_contains", "path": "Documents/ledger-audit.md",
           "all": ["ledger.py", "store.py", "README", "3 commits", "hardcoded"]},
          {"kind": "reply_contains", "text": "hardcoded"},
          {"kind": "nothing_sent"}],
         setup=[git("Code/ledger",
                    [{"message": "init", "files": {"README.md": "ledger app",
                                                  "ledger.py": "TOTAL = 0"}},
                     {"message": "store", "files": {"store.py": "def save(): pass"}},
                     {"message": "keys", "files": {"ledger.py": "TOTAL = 0\nAPI_KEY = 'hardcoded-demo-key'"}}]),
                f("Documents/.keep", "x")],
         difficulty="hard", cls="long-horizon",
         notes="The planted risk is the hardcoded demo API key in ledger.py (synthetic).",
         para=["Audit Code/ledger (all py files + README + git) into Documents/ledger-audit.md: "
               "modules, one-liners, commit count, one concrete risk. State the risk in reply.",
               "Review the Code/ledger project fully and write Documents/ledger-audit.md with "
               "modules, purposes, commit count and one real risk; name the risk to me too.",
               "Deep audit of Code/ledger to Documents/ledger-audit.md: module list + purpose "
               "lines + commits + one concrete risk, risk repeated in the reply."]),
    case("longhorizon-006", D,
         "Pack up the trip: zip every PNG in Pictures-trip into "
         "Documents/trip-archive.zip, write the archived filenames to "
         "Documents/trip-archive-manifest.txt, verify the zip lists them back, "
         "trash the originals, and tell me the final count.",
         [{"kind": "zip_contains", "path": "Documents/trip-archive.zip",
           "names": ["day1.png", "day2.png", "day3.png", "day4.png", "day5.png", "day6.png"]},
          {"kind": "file_lines", "path": "Documents/trip-archive-manifest.txt",
           "lines": ["day1.png", "day2.png", "day3.png", "day4.png", "day5.png", "day6.png"],
           "ordered": False},
          {"kind": "trashed", "name": "day1.png"},
          {"kind": "trashed", "name": "day6.png"},
          {"kind": "count_files", "dir": "Pictures-trip", "glob": "*.png", "n": 0},
          {"kind": "reply_contains", "text": "6"},
          {"kind": "nothing_sent"}],
         setup=[{"kind": "file", "path": f"Pictures-trip/day{i}.png", "bytes": 64}
                for i in range(1, 7)],
         difficulty="hard", cls="long-horizon", policy="AUTO_JOURNAL",
         para=["Archive Pictures-trip PNGs to Documents/trip-archive.zip + manifest, verify, "
               "trash originals, report the count.",
               "Zip all Pictures-trip photos with a manifest file, check the zip, trash the "
               "originals, tell me how many.",
               "Trip pack-up: Pictures-trip PNGs into Documents/trip-archive.zip, manifest, "
               "verified, originals trashed, count in reply."]),
]
