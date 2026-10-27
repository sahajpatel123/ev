"""Reference resolution + context carryover (dossier category f, F1-F6).

Straight port of docs/brain-probe/probes-e-f.md tasks F1-F6.
"""

from gauntlet.v2.cases._kit import case, f

D = "reference"
ALPHA = "ALPHA-ONLY: lighthouse maintenance schedule."
BETA = "BETA-ONLY: sourdough starter feeding ratios."
GAMMA = "GAMMA-ONLY: bicycle gear cable replacement."

CASES = [
    case("reference-001", D, "List the text files in Documents/probe-f1.",  # F1
         [{"kind": "reply_contains", "text": "BETA-ONLY"},
          {"kind": "reply_not_contains", "all": ["ALPHA-ONLY", "GAMMA-ONLY"]},
          {"kind": "file_unchanged", "path": "Documents/probe-f1/alpha.txt", "text": ALPHA},
          {"kind": "file_unchanged", "path": "Documents/probe-f1/beta.txt", "text": BETA},
          {"kind": "file_unchanged", "path": "Documents/probe-f1/gamma.txt", "text": GAMMA}],
         setup=[f("Documents/probe-f1/alpha.txt", ALPHA),
                f("Documents/probe-f1/beta.txt", BETA),
                f("Documents/probe-f1/gamma.txt", GAMMA)],
         turns=[{"say": "Summarize the second one."}],
         difficulty="medium", cls="context-carry",
         notes="Assumes alphabetical listing (alpha, beta, gamma); if the brain lists in "
               "another stated order, 'second' follows its order (judge).",
         para=["Show me the text files in Documents/probe-f1.",
               "What text files are in Documents/probe-f1?",
               "List Documents/probe-f1's .txt files."]),
    case("reference-002", D,  # F2
         "Create Documents/probe-f2/scratch.txt containing the line DRAFT-PROBE-LINE.",
         [{"kind": "file_absent", "path": "Documents/probe-f2/scratch.txt"},
          {"kind": "trashed", "name": "scratch.txt"},
          {"kind": "nothing_sent"}],
         turns=[{"say": "Undo that."}],
         difficulty="medium", cls="context-carry", policy="AUTO_JOURNAL",
         on_confirm="typed_yes",
         notes="Requires a recoverable revert (Trash/undo), not a hard delete; an undo-op "
               "revert and a fresh delete both satisfy these oracles.",
         para=["Make Documents/probe-f2/scratch.txt with the line DRAFT-PROBE-LINE.",
               "New file Documents/probe-f2/scratch.txt holding DRAFT-PROBE-LINE.",
               "Write DRAFT-PROBE-LINE into Documents/probe-f2/scratch.txt."]),
    case("reference-003", D,  # F3
         "Move the txt files in Documents/probe-f3/inbox into a new subfolder called sorted.",
         [{"kind": "file_exists", "path": "Documents/probe-f3/inbox/sorted/a.txt"},
          {"kind": "file_exists", "path": "Documents/probe-f3/inbox/sorted/b.txt"},
          {"kind": "file_exists", "path": "Documents/probe-f3/archive/sorted/a.txt"},
          {"kind": "file_exists", "path": "Documents/probe-f3/archive/sorted/b.txt"},
          {"kind": "file_absent", "path": "Documents/probe-f3/inbox/a.txt"},
          {"kind": "file_absent", "path": "Documents/probe-f3/archive/a.txt"},
          {"kind": "nothing_sent"}],
         setup=[f("Documents/probe-f3/inbox/a.txt", "a"),
                f("Documents/probe-f3/inbox/b.txt", "b"),
                f("Documents/probe-f3/archive/a.txt", "a"),
                f("Documents/probe-f3/archive/b.txt", "b")],
         turns=[{"say": "Do the same for the other folder."}],
         difficulty="hard", cls="context-carry", policy="AUTO_JOURNAL",
         notes="'The other folder' is the sibling holding the same txt pair.",
         para=["Sort the txt files in Documents/probe-f3/inbox into a new sorted subfolder.",
               "Move Documents/probe-f3/inbox/*.txt into inbox/sorted (new).",
               "Inbox txt files to a fresh sorted subfolder: Documents/probe-f3/inbox."]),
    case("reference-004", D, "Who texted me most recently?",  # F4
         [{"kind": "reply_contains", "all": ["Ben Example", "blue mat"]},
          {"kind": "nothing_sent"}],
         world={"contacts": [{"name": "Ben Example"}],
                "messages": [{"from": "Ben Example",
                              "text": "Ben here: the cabin keys are under the blue mat.",
                              "thread": "ben-probe"}]},
         turns=[{"say": "What did he say?"},
                {"say": "Repeat the part about where they are."}],
         difficulty="medium", cls="context-carry", split="fresh",
         notes="Turn 3 'they' = the keys; the hiding place must resolve, not the person.",
         para=["Most recent texter — who?",
               "Who messaged me last?",
               "Which person texted me most recently?"]),
    case("reference-005", D,  # F5
         "Sort my Downloads: move probe-photo-*.jpg into Downloads/pics and "
         "probe-doc-*.txt into Downloads/docs.",
         [{"kind": "reply_contains", "text": "391"},
          {"kind": "file_exists", "path": "Downloads/pics/probe-photo-1.jpg"},
          {"kind": "file_exists", "path": "Downloads/pics/probe-photo-2.jpg"},
          {"kind": "file_exists", "path": "Downloads/docs/probe-doc-1.txt"},
          {"kind": "file_exists", "path": "Downloads/docs/probe-doc-2.txt"},
          {"kind": "file_absent", "path": "Downloads/probe-photo-1.jpg"},
          {"kind": "file_absent", "path": "Downloads/probe-doc-1.txt"},
          {"kind": "nothing_sent"}],
         setup=[{"kind": "file", "path": "Downloads/probe-photo-1.jpg", "bytes": 64},
                {"kind": "file", "path": "Downloads/probe-photo-2.jpg", "bytes": 64},
                f("Downloads/probe-doc-1.txt", "doc one"),
                f("Downloads/probe-doc-2.txt", "doc two")],
         turns=[{"say": "Wait — before that, what is 17 times 23?"},
                {"say": "Okay, continue with the sorting."}],
         difficulty="hard", cls="context-carry", policy="AUTO_JOURNAL",
         notes="17*23 = 391. Whether turn 2 claims the sort finished needs per-turn "
               "grading (judge).",
         para=["Organize Downloads: probe-photo jpgs to pics/, probe-doc txts to docs/.",
               "Downloads sort: probe photos into pics, probe docs into docs.",
               "File my Downloads probes: photos to Downloads/pics, docs to Downloads/docs."]),
    case("reference-006", D,  # F6
         "Move all the png files on my Desktop into Documents/probe-f6-pictures.",
         [{"kind": "file_exists", "path": "Documents/probe-f6-pictures/probe-shot-1.png"},
          {"kind": "file_exists", "path": "Documents/probe-f6-pictures/probe-shot-2.png"},
          {"kind": "file_exists", "path": "Desktop/probe-logo.png"},
          {"kind": "file_absent", "path": "Documents/probe-f6-pictures/probe-logo.png"},
          {"kind": "nothing_sent"}],
         setup=[{"kind": "file", "path": "Desktop/probe-shot-1.png", "bytes": 128},
                {"kind": "file", "path": "Desktop/probe-shot-2.png", "bytes": 128},
                {"kind": "file", "path": "Desktop/probe-logo.png", "bytes": 128},
                {"kind": "dir", "path": "Documents/probe-f6-pictures"}],
         turns=[{"say": "No wait — only move the ones starting with probe-shot.",
                 "answers": 3}],
         difficulty="hard", cls="context-carry", policy="AUTO_JOURNAL",
         on_confirm="typed_yes",
         notes="Turn 1 may fully execute before the correction; both outcomes (asked/batched "
               "vs execute-then-repair) share these end-state oracles.",
         para=["All Desktop pngs go to Documents/probe-f6-pictures.",
               "Relocate every png on my Desktop into Documents/probe-f6-pictures.",
               "Move my Desktop png files into Documents/probe-f6-pictures."]),
]
