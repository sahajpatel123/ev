#!/bin/zsh
# Diagnose why EV is missing from System Settings > Privacy & Security, or why
# permissions granted there do not stick.
#
# Prints only. It never resets anything: the tccutil commands are shown so you
# can run them yourself.
#
# Usage: ./scripts/doctor.sh

set -euo pipefail

BUNDLE_ID="com.ev.suit"
APP="/Applications/EV.app"
USER_APP="$HOME/Applications/EV.app"
TCC_DB="$HOME/Library/Application Support/com.apple.TCC/TCC.db"

ok() { print -r -- "✅ $1"; }
bad() { print -r -- "❌ $1"; }
fix() { print -r -- "   fix: $1"; }
section() { print -r -- ""; print -r -- "== $1"; }

section "install location"
INSTALLED=""
if [ -d "$APP" ]; then
    INSTALLED="$APP"
    ok "$APP exists"
elif [ -d "$USER_APP" ]; then
    INSTALLED="$USER_APP"
    ok "$USER_APP exists (per-user install)"
else
    bad "no EV.app in /Applications or ~/Applications"
    fix "./scripts/package.sh && ./scripts/install.sh"
fi

section "running copy"
# Ends at a space or end-of-line so the notification helper is not reported as
# a second running EV.
RUNNING="$(pgrep -fl 'EV\.app/Contents/MacOS/EV( |$)' 2>/dev/null || true)"
if [ -z "$RUNNING" ]; then
    print -r -- "-- EV is not running"
    fix "launch the installed copy: open \"${INSTALLED:-$APP}\""
else
    print -r -- "$RUNNING"
    FIRST_LINE="${RUNNING%%$'\n'*}"
    RUNNING_PATH="${FIRST_LINE#* }"
    case "$RUNNING_PATH" in
        *AppTranslocation*|/private/var/folders/*)
            bad "the running copy is translocated (Gatekeeper path randomisation)"
            fix "quit EV, run: xattr -dr com.apple.quarantine \"${INSTALLED:-$APP}\", move the bundle in Finder, relaunch. Translocated launches get a new identity every time, so no grant survives."
            ;;
        *)
            if [ -n "$INSTALLED" ] && [ "${RUNNING_PATH#$INSTALLED}" = "$RUNNING_PATH" ]; then
                bad "a copy outside $INSTALLED is running: $RUNNING_PATH"
                fix "quit it and launch \"$INSTALLED\" — TCC treats each location/signature as a different app"
            else
                ok "running from the installed bundle"
            fi
            ;;
    esac
fi

section "code signature"
if [ -n "$INSTALLED" ]; then
    SIGN_INFO="$(codesign -dv --verbose=4 "$INSTALLED" 2>&1 || true)"
    IDENTIFIER="$(print -r -- "$SIGN_INFO" | sed -n 's/^Identifier=//p' | head -1)"
    FLAGS="$(print -r -- "$SIGN_INFO" | sed -n 's/^CodeDirectory .*flags=\([^ ]*\).*/\1/p' | head -1)"
    AUTHORITY="$(print -r -- "$SIGN_INFO" | sed -n 's/^Authority=//p' | head -1)"
    if [ -z "$IDENTIFIER" ]; then
        bad "unsigned (or unreadable signature)"
        fix "./scripts/package.sh — TCC cannot record a grant for unsigned code"
    else
        ok "identifier: $IDENTIFIER"
        case "$FLAGS" in
            *adhoc*)
                bad "ad-hoc signature (flags=$FLAGS)"
                fix "ad-hoc identities are the cdhash, so they change on every rebuild and old grants stop applying. EV_CODESIGN_IDENTITY=\"Developer ID Application: ...\" ./scripts/package.sh"
                ;;
            *)
                ok "stable identity: ${AUTHORITY:-${FLAGS:-signed}}"
                ;;
        esac
    fi
    if codesign --verify --strict "$INSTALLED" 2>/dev/null; then
        ok "signature verifies (--strict)"
    else
        bad "signature does not verify"
        fix "repackage: ./scripts/package.sh (the helper in Contents/MacOS must be signed before the app)"
    fi
else
    print -r -- "-- skipped, nothing installed"
fi

section "quarantine"
if [ -n "$INSTALLED" ]; then
    QUARANTINE="$(xattr -l "$INSTALLED" 2>/dev/null | grep -c 'com.apple.quarantine' || true)"
    if [ "${QUARANTINE:-0}" -eq 0 ]; then
        ok "com.apple.quarantine is clear"
    else
        bad "com.apple.quarantine is set"
        fix "xattr -dr com.apple.quarantine \"$INSTALLED\" — this attribute is what triggers App Translocation"
    fi
else
    print -r -- "-- skipped, nothing installed"
fi

section "usage strings"
if [ -n "$INSTALLED" ]; then
    for key in \
        NSMicrophoneUsageDescription \
        NSSpeechRecognitionUsageDescription \
        NSCameraUsageDescription \
        NSScreenCaptureUsageDescription \
        NSAppleEventsUsageDescription \
        NSContactsUsageDescription \
        NSCalendarsUsageDescription \
        NSCalendarsFullAccessUsageDescription \
        NSRemindersUsageDescription \
        NSRemindersFullAccessUsageDescription \
        NSBluetoothAlwaysUsageDescription \
        NSLocationWhenInUseUsageDescription
    do
        if /usr/libexec/PlistBuddy -c "Print :$key" "$INSTALLED/Contents/Info.plist" >/dev/null 2>&1; then
            ok "$key present"
        else
            bad "$key missing"
            fix "macOS kills the process instead of prompting when the string is missing, so EV never reaches the Privacy list. Add it to Resources/Info.plist and repackage."
        fi
    done
else
    print -r -- "-- skipped, nothing installed"
fi

section "TCC records for $BUNDLE_ID"
# The user TCC database is itself protected; reading it needs Full Disk Access
# for the terminal app. A failure here says nothing about EV.
if [ ! -f "$TCC_DB" ]; then
    print -r -- "-- no user TCC database at $TCC_DB"
elif ! TCC_ROWS="$(sqlite3 "$TCC_DB" "select service,client,auth_value from access where client like '%ev%'" 2>/dev/null)"; then
    print -r -- "-- cannot read the TCC database (expected without Full Disk Access)"
    fix "grant Full Disk Access to your terminal in System Settings > Privacy & Security, or just read the panes in System Settings directly"
elif [ -z "$TCC_ROWS" ]; then
    bad "no TCC rows mention EV — EV has never triggered a permission request"
    fix "launch EV and click Permissions… > \"Grant permissions\". Reading a permission's status does not register the app; only a request does."
else
    print -r -- "$TCC_ROWS"
    print -r -- "   auth_value: 0 = denied, 2 = allowed, 3 = limited"
fi

section "re-arming prompts"
print -r -- "A denied permission never prompts again; EV stays in the list with its"
print -r -- "switch off. Flip the switch, or reset the decision and ask again:"
print -r -- "  tccutil reset Microphone $BUNDLE_ID"
print -r -- "  tccutil reset SpeechRecognition $BUNDLE_ID"
print -r -- "  tccutil reset Camera $BUNDLE_ID"
print -r -- "  tccutil reset ScreenCapture $BUNDLE_ID"
print -r -- "  tccutil reset AppleEvents $BUNDLE_ID"
print -r -- "  tccutil reset AddressBook $BUNDLE_ID"
print -r -- "  tccutil reset Calendar $BUNDLE_ID"
print -r -- "  tccutil reset Reminders $BUNDLE_ID"
print -r -- "  tccutil reset BluetoothAlways $BUNDLE_ID"
print -r -- "  tccutil reset ListenEvent $BUNDLE_ID"
print -r -- "  tccutil reset Accessibility $BUNDLE_ID"
print -r -- "  tccutil reset Location $BUNDLE_ID"
print -r -- "  tccutil reset SystemPolicyAllFiles $BUNDLE_ID"
print -r -- "  tccutil reset All $BUNDLE_ID"
print -r -- "Quit EV before resetting, then relaunch and click \"Grant permissions\"."
print -r -- "Do not open a Privacy pane until after Grant permissions — an empty"
print -r -- "list means EV has not requested that service yet, not that it is hidden."
print -r -- "Notifications are not TCC-backed: change them in System Settings >"
print -r -- "Notifications > EV. Full Disk Access has no prompt: add EV.app with +."
