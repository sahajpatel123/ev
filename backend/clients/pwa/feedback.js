/* Visual feedback only. Optional vibrate if the browser actually supports it. No fake click sounds. */
(function (root) {
  const EVENTS = {
    tapPrimary: true,
    conversationStart: true,
    conversationStop: true,
    toolSuccess: true,
    toolFailure: true,
    cameraCapture: true,
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

  root.EvieFeedback = {
    visualPress: visualPress,
    visualSuccess: visualSuccess,
    visualWarning: visualWarning,
    haptic: haptic,
    hapticAvailable: hapticAvailable,
    emit: emit,
  };
})(typeof window !== "undefined" ? window : globalThis);
