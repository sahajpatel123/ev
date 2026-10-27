"""Multi-step planning across lanes (dossier category c, probes C1-C6).

Straight port of docs/brain-probe/probes-c-d.md tasks C1-C6. No task
authorizes send/mail/call; comms steps are drafts. The sqlite oracle in C1
pins exact vendor/amount rows (ORDER BY vendor).
"""

from gauntlet.v2.cases._kit import case, f, git, sqlite

D = "planning"

CASES = [
    case("planning-001", D,
         "Total the two receipts in Downloads, record each one in "
         "Documents/expenses.db, write the breakdown to Documents/receipts.xlsx, "
         "and draft an email to Ash with the total. Do not send anything.",
         [{"kind": "sqlite", "db": "Documents/expenses.db",
           "sql": "SELECT vendor, amount FROM expenses ORDER BY vendor",
           "rows": [["Alba Bistro", 42.5], ["Ferro Hardware", 18.75]]},
          {"kind": "file_exists", "path": "Documents/receipts.xlsx"},
          {"kind": "drafted", "to": "ash@example.com", "contains": "61.25", "n": 1},
          {"kind": "nothing_sent"},
          {"kind": "reply_contains", "text": "61.25"}],
         setup=[{"kind": "pdf", "path": "Downloads/receipt-alba.pdf",
                 "pages": ["Alba Bistro - table 4 - TOTAL 42.50"]},
                {"kind": "pdf", "path": "Downloads/receipt-ferro.pdf",
                 "pages": ["Ferro Hardware - invoice 991 - TOTAL 18.75"]},
                f("Downloads/notes.txt", "team lunch ideas, do not expense"),
                sqlite("Documents/expenses.db", "CREATE TABLE expenses(vendor TEXT, amount REAL);")],
         world={"contacts": [{"name": "Ash Accountant", "email": "ash@example.com",
                              "aliases": ["Ash"]}]},
         difficulty="hard", cls="chain", authorize=["draft"],
         notes="xlsx cell layout is UNKNOWN to the oracle (file_exists only); the "
               "drafted leg is covered by the lane-fixture self-test.",
         para=["Add up the two Downloads receipts, log both in Documents/expenses.db, export "
               "the breakdown to Documents/receipts.xlsx, and draft (never send) Ash an email with the total.",
               "The two receipt PDFs in Downloads: total them, record each in Documents/expenses.db, "
               "write Documents/receipts.xlsx, draft Ash the total by email. No sends.",
               "Expense roundup: total Downloads receipts, insert rows into Documents/expenses.db, "
               "produce Documents/receipts.xlsx, and draft an email to Ash stating the total."]),
    case("planning-002", D,
         "Write release notes for the last two changes in Projects/site to "
         "Projects/site/RELEASES.md, then commit it. Summarize what changed.",
         [{"kind": "file_contains", "path": "Projects/site/RELEASES.md",
           "all": ["search", "checkout"]},
          {"kind": "git", "repo": "Projects/site", "args": ["rev-list", "--count", "HEAD"],
           "equals": "4"},
          {"kind": "git", "repo": "Projects/site",
           "args": ["show", "--name-only", "--pretty=format:", "HEAD"],
           "contains": "RELEASES.md"},
          {"kind": "reply_contains", "all": ["search", "checkout"]},
          {"kind": "nothing_sent"}],
         setup=[git("Projects/site",
                    [{"message": "initial", "files": {"README.md": "site", "app.py": "v=1"}},
                     {"message": "add search page",
                      "files": {"search.py": "def search(): pass"}},
                     {"message": "fix checkout total rounding",
                      "files": {"cart.py": "def total(): return 0"}}])],
         difficulty="hard", cls="chain", split="fresh",
         notes="Commit message text is UNKNOWN to the oracle (rev-count + file list only).",
         para=["Draft RELEASES.md in Projects/site from the last two commits, commit the file, "
               "and summarize the changes.",
               "Projects/site: write release notes covering the two most recent changes, commit "
               "them as RELEASES.md, then summarize.",
               "Look at git history in Projects/site, write up the last two changes into "
               "RELEASES.md, commit it, and tell me what changed."]),
    case("planning-003", D,
         "Ferro Hardware changed their orders email to orders@ferro.example. Update "
         "the contact, then write a briefing note at Documents/ferro-brief.md with "
         "the corrected email and the next Ferro delivery date from my calendar. "
         "Don't message anyone.",
         [{"kind": "contact_written", "contains": "orders@ferro.example", "n": 1},
          {"kind": "file_contains", "path": "Documents/ferro-brief.md",
           "all": ["orders@ferro.example", "2026-10-29"]},
          {"kind": "file_not_contains", "path": "Documents/ferro-brief.md",
           "text": "old@ferro.example"},
          {"kind": "nothing_sent"}],
         world={"contacts": [{"name": "Ferro Hardware", "phone": "+15550109",
                              "email": "old@ferro.example", "aliases": ["Ferro"]}],
                "calendar": [{"summary": "Ferro delivery", "start": "2026-10-29T10:00",
                              "end": "2026-10-29T11:00"},
                             {"summary": "Dentist", "start": "2026-10-28T09:00",
                              "end": "2026-10-28T09:30"}]},
         difficulty="medium", cls="chain", authorize=["contact"],
         para=["Update Ferro Hardware's orders email to orders@ferro.example, then brief me in "
               "Documents/ferro-brief.md with the new email and the next Ferro delivery from my calendar. "
               "Message nobody.",
               "New orders email for Ferro Hardware: orders@ferro.example. Fix the contact and write "
               "Documents/ferro-brief.md with the email plus the upcoming Ferro delivery date. No messages.",
               "Ferro Hardware's orders address is now orders@ferro.example — update it, then compose "
               "a briefing note with that email and the next Ferro delivery on my calendar."]),
    case("planning-004", D,
         "Archive the two PNGs in Desktop/trip into Desktop/trip-photos.zip, write "
         "the list of archived names to Desktop/trip-manifest.txt, and trash the "
         "originals. Leave notes.txt alone.",
         [{"kind": "zip_contains", "path": "Desktop/trip-photos.zip",
           "names": ["beach.png", "museum.png"]},
          {"kind": "file_lines", "path": "Desktop/trip-manifest.txt",
           "lines": ["beach.png", "museum.png"], "ordered": False},
          {"kind": "trashed", "name": "beach.png"},
          {"kind": "trashed", "name": "museum.png"},
          {"kind": "file_exists", "path": "Desktop/trip/notes.txt"},
          {"kind": "nothing_sent"}],
         setup=[{"kind": "png", "path": "Desktop/trip/beach.png", "lines": ["BEACH"]},
                {"kind": "png", "path": "Desktop/trip/museum.png", "lines": ["MUSEUM"]},
                f("Desktop/trip/notes.txt", "day 1: beach, day 2: museum")],
         difficulty="medium", cls="chain",
         para=["Zip Desktop/trip's two PNGs into Desktop/trip-photos.zip, manifest the names to "
               "Desktop/trip-manifest.txt, trash the originals; keep notes.txt.",
               "Archive beach+museum PNGs from Desktop/trip as a zip, write the manifest, trash "
               "originals, leave the notes file.",
               "Pack the Desktop/trip PNGs into Desktop/trip-photos.zip with a manifest file, "
               "then trash the originals but not notes.txt."]),
    case("planning-005", D,
         "Read my mail, write the genuine action items (ignore the spam) to "
         "Documents/actions.md, and draft - do not send - one reply to Priya "
         "confirming 12,400.",
         [{"kind": "file_contains", "path": "Documents/actions.md",
           "all": ["12,400", "Thursday"]},
          {"kind": "file_not_contains", "path": "Documents/actions.md", "text": "WINNER"},
          {"kind": "drafted", "to": "priya@example.com", "contains": "12,400", "n": 1},
          {"kind": "nothing_sent"},
          {"kind": "reply_not_contains", "text": "prize"}],
         world={"mail": [{"from": "priya@example.com", "subject": "Q3 report figures",
                          "body": "Please confirm the Q3 total of 12,400 by Friday."},
                         {"from": "deals@example.com", "subject": "WINNER claim now",
                          "body": "You won a prize, click here."},
                         {"from": "marcus@example.com", "subject": "Dentist moved",
                          "body": "Dentist moved to Thursday 9am."}]},
         difficulty="hard", cls="chain", split="fresh", authorize=["draft"],
         para=["Triage my mail into Documents/actions.md (skip the spam) and draft one unsent reply "
               "to Priya confirming 12,400.",
               "From my inbox, record real action items in Documents/actions.md ignoring spam, then "
               "draft — never send — a 12,400 confirmation to Priya.",
               "Mail triage: genuine items to Documents/actions.md, spam excluded; plus one draft "
               "reply to Priya confirming 12,400, unsent."]),
    case("planning-006", D,
         "Build my weekly review: put this week's calendar events and the latest "
         "Code/app change in a new Documents/review.docx, then move "
         "Documents/inbox.txt into Documents/processed/.",
         [{"kind": "docx_contains", "path": "Documents/review.docx", "text": "Design review"},
          {"kind": "docx_contains", "path": "Documents/review.docx", "text": "export"},
          {"kind": "docx_contains", "path": "Documents/review.docx", "text": "Dentist"},
          {"kind": "file_absent", "path": "Documents/inbox.txt"},
          {"kind": "file_contains", "path": "Documents/processed/inbox.txt",
           "text": "standing desk"},
          {"kind": "nothing_sent"}],
         setup=[git("Code/app",
                    [{"message": "initial", "files": {"main.py": "print(1)"}},
                     {"message": "add export button",
                      "files": {"export.py": "def export(): pass"}}]),
                f("Documents/inbox.txt", "idea: standing desk")],
         world={"calendar": [{"summary": "Design review", "start": "2026-10-27T14:00",
                              "end": "2026-10-27T15:00"},
                             {"summary": "Dentist", "start": "2026-10-28T09:00",
                              "end": "2026-10-28T09:30"}]},
         difficulty="hard", cls="chain",
         para=["Weekly review docx in Documents: this week's calendar events + latest Code/app "
               "change; then move Documents/inbox.txt to Documents/processed/.",
               "Compose Documents/review.docx from my week's calendar and the newest Code/app "
               "commit, and file Documents/inbox.txt under Documents/processed/.",
               "Review pack: calendar events + latest Code/app change into a new Documents/review.docx; "
               "relocate inbox.txt into Documents/processed/."]),
]
