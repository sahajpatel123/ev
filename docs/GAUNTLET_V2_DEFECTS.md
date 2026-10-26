# Observed Evie defects (pre-baseline; fix in Phase 4)
F1 local fast path: "Rename X to report.txt" parsed as a create -> asks "What should I say to report.txt.?" (files-003)
F2 files lane gate: lane_root() = first D1 root only; reads/writes in Documents/Downloads/Code/Projects via files.act ask CONFIRM (files-002)
F3 local fast path: append a line -> appends the whole utterance text (files-008)
F4 local fast path: "how many lines in X" -> tries to open X in an app (files-013)
F5 move PDFs within D1 roots parks CONFIRM (files-004) - over-confirmation
F7 files.act bulk returns verified+"done" with n=0 (files-010) - lane false receipt
F8 "zip folder" routed to computer.perform_effect (GUI) not shell (files-015)
F9 files.act list ignores path (lists root) (files-010)
F10 voice "yes" on a parked send re-calls life.send -> new ticket each time (smoke-send-voice-yes)
F11 reply "Hi there, on the note." after creating a file (smoke-create-file scripted brain)
