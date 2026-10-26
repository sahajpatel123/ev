# Owner-run live checks (HANDS)

These run on your real desktop and real windows. They are scripts I wrote and did **not** run against your
setup. Each is a dry run first; anything that opens, closes or presses something asks you to confirm.

```sh
scripts/ev-hands check permissions          # also: python -m app.fabric.skills.check permissions
scripts/ev-hands check t1                   # plan only
scripts/ev-hands check t1 --apply           # build it, after you press Return
scripts/ev-hands check t2 --watch 120       # wait up to 2 minutes for an ad, then skip it
```

(`ev hands check ...` is the same entry point once the `ev` CLI registers it; that file belongs to the brain session.)

## permissions

Reads every permission without triggering a macOS prompt: Accessibility, Screen Recording, Automation per app (Ghostty,
Chrome, Safari, Finder, System Events, Music), "Allow JavaScript from Apple Events" for each running browser, the Ghostty
scripting dictionary, the OCR helper and each coding CLI's resume syntax. Missing items print the exact Settings path.

Things only you can do: Chrome **View > Developer > Allow JavaScript from Apple Events**; Safari **Settings > Developer >
Allow JavaScript from Apple Events** (turn on the Develop menu in Advanced first); Accessibility and Automation for Evie.app
(or for the terminal that runs these checks).

## T1 - Ghostty layout from a screenshot

1. Make Ghostty look the way you want it and press Cmd-Shift-4 over the window (or any screenshot of it).
2. `scripts/ev-hands check t1` prints the inferred split layout as a picture and, for every pane: the working directory
   (from the session's own record), the command that will run (regenerated from the verified resume syntax, never copied
   from the image), how the id was matched (exact / corrected look-alikes / prefix completed), and any warning
   (ambiguous id, unknown id, session already open elsewhere, folder missing). Panes it cannot confirm stay plain shells.
3. `--apply` opens **one new tab in your frontmost Ghostty window** (a new window if none), builds the splits, sets the
   sizes, runs the resume commands, and reads every pane back (folder, command shown, session process started with that id).
4. Undo: close that tab. Nothing else is touched.

Pass criteria: the picture matches your screenshot, every printed command is one you recognise, `RESULT: ok=True verified=True`.
If a session is already open in another terminal it is **not** started a second time unless you ask for it.

## T2 - skip the ad

1. In Chrome (or Safari), play a YouTube video that shows an ad; it can be in a background tab or window.
2. `scripts/ev-hands check t2 --watch 120`. It prints which tab is playing, the ad state, then skips.

Pass criteria: `SAY: Skipped the ad.` within about a second of the skip button appearing, `verified=True`, the ad gone.
An unskippable ad must print `That ad can't be skipped; about N seconds left.` and press nothing. With the screen locked
or the display asleep it must say exactly that and do nothing (test by locking the Mac while this runs).
If it prints `js_disabled`, the message names the browser setting to turn on.
