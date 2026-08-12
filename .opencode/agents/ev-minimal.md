---
description: EV chat bridge. Carries no coding preamble and no tools; EV supplies its own system prompt on every request.
mode: primary
temperature: 0.7
permission:
  read: deny
  edit: deny
  glob: deny
  grep: deny
  list: deny
  bash: deny
  task: deny
  external_directory: deny
  todowrite: deny
  webfetch: deny
  websearch: deny
  lsp: deny
  skill: deny
  question: deny
  doom_loop: deny
tools:
  "*": false
---

Follow the system instructions supplied with the request.
