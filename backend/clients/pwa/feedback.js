/* Visual feedback only. Optional vibrate if the browser actually supports it. No fake click sounds. */
(function (root) {
  // Base UI events (unchanged contract).
  const EVENTS = {
    tapPrimary: true,
    conversationStart: true,
    conversationStop: true,
    toolSuccess: true,
    toolFailure: true,
    cameraCapture: true,
    // Cycle 1 (iPhone-only): voice-turn haptics beyond chat. Distinct from
    // base UI taps so 16 Pro / SE Taptic Engines can tell listening, speaking,
    // turn-done, barge-in, and reconnect apart without looking at the screen.
    listeningStart: true,
    speakingStart: true,
    turnDone: true,
    bargeIn: true,
    reconnected: true,
  };

  function hapticAvailable() {
    return typeof navigator !== "undefined" && typeof navigator.vibrate === "function";
  }

  function visualPress(el) {
    if (!el) return;
    el.classList.add("is-pressed");
    window.setTimeout(function () { el.classList.remove("is-pressed"); }, 140);
  }

  function visualSuccess(el) {
    if (!el) return;
    el.classList.add("is-ok");
    window.setTimeout(function () { el.classList.remove("is-ok"); }, 420);
  }

  function visualWarning(el) {
    if (!el) return;
    el.classList.add("is-warn");
    window.setTimeout(function () { el.classList.remove("is-warn"); }, 520);
  }

  function haptic(pattern) {
    if (window.EvieNativeShell && window.EvieNativeShell.post) {
      window.EvieNativeShell.post({ type: "haptic", event: "selection" });
      return true;
    }
    if (!hapticAvailable()) return false;
    try {
      navigator.vibrate(pattern || 10);
      return true;
    } catch (_err) {
      return false;
    }
  }

  function emit(name, el) {
    if (!EVENTS[name]) return;
    visualPress(el);
    if (name === "toolSuccess") visualSuccess(el);
    if (name === "toolFailure") visualWarning(el);
  }

  // iPhone Taptic patterns per voice event. Short = 10ms tick, double =
  // confirm, triple-short = interruption, long-short = reconnect cue.
  // Honors prefers-reduced-motion: no vibration when the owner asked for less.
  const VOICE_PATTERNS = {
    listeningStart: 12,
    speakingStart: [12, 40, 12],
    turnDone: [10, 30, 18],
    bargeIn: [25, 30, 25, 30, 25],
    reconnected: [60, 40, 20],
  };

  function reducedMotion() {
    try {
      return (
        typeof window !== "undefined" &&
        typeof window.matchMedia === "function" &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches
      );
    } catch (_err) {
      return false;
    }
  }

  // Cycle 1 entry point: hapticEvent("turnDone", el?) — safe no-op on Mac
  // browsers without navigator.vibrate and inside EvieShell via post().
  function hapticEvent(name, el) {
    if (!name || (!EVENTS[name] && !VOICE_PATTERNS[name])) return false;
    if (el) visualPress(el);
    if (reducedMotion()) return false;
    const pattern = VOICE_PATTERNS[name] || 10;
    if (window.EvieNativeShell && window.EvieNativeShell.post) {
      try {
        window.EvieNativeShell.post({ type: "haptic", event: name });
        return true;
      } catch (_err) {
        return false;
      }
    }
    return haptic(pattern);
  }

  root.EvieFeedback = {
    visualPress: visualPress,
    visualSuccess: visualSuccess,
    visualWarning: visualWarning,
    haptic: haptic,
    hapticAvailable: hapticAvailable,
    hapticEvent: hapticEvent,
    voicePatterns: VOICE_PATTERNS,
    emit: emit,
  };
})(typeof window !== "undefined" ? window : globalThis);
