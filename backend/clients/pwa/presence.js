/* Evie Veil — Presence Entity. Asymmetric folded membranes, not a circle. */
(function (root) {
  function rgb(value, fallback) {
    const v = (value || "").trim();
    if (v.charAt(0) === "#") {
      const hex = v.length === 4
        ? "#" + v.charAt(1) + v.charAt(1) + v.charAt(2) + v.charAt(2) + v.charAt(3) + v.charAt(3)
        : v;
      const num = parseInt(hex.slice(1), 16);
      return [(num >> 16) & 255, (num >> 8) & 255, num & 255];
    }
    return fallback;
  }

  function EviePresence(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d", { alpha: true, desynchronized: true });
    this.state = "idle";
    this.amp = 0;
    this.targetAmp = 0;
    this.t = 0;
    this.raf = 0;
    this.reduced = false;
    this.visible = true;
    this.last = 0;
    this.quality = "high";
    this.pearl = [236, 228, 214];
    this.filament = [196, 168, 132];
    this.ink = [28, 26, 22];
    this.refreshTheme();
    const self = this;
    this._onVis = function () {
      self.visible = document.visibilityState !== "hidden";
      if (self.visible) self.start();
      else self.stopLoop();
    };
    document.addEventListener("visibilitychange", this._onVis);
    if (window.matchMedia) {
      window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () {
        self.refreshTheme();
      });
    }
  }

  EviePresence.prototype.refreshTheme = function refreshTheme() {
    const css = getComputedStyle(document.documentElement);
    this.pearl = rgb(css.getPropertyValue("--veil-pearl"), this.pearl);
    this.filament = rgb(css.getPropertyValue("--veil-filament"), this.filament);
    this.ink = rgb(css.getPropertyValue("--text-primary"), this.ink);
  };

  EviePresence.prototype.setReduced = function setReduced(on) {
    this.reduced = !!on;
    this.quality = on ? "reduced" : "high";
  };

  EviePresence.prototype.setState = function setState(state, amp) {
    this.state = state || "idle";
    if (typeof amp === "number") this.targetAmp = Math.max(0, Math.min(1, amp));
  };

  EviePresence.prototype.setAmp = function setAmp(amp) {
    this.targetAmp = Math.max(0, Math.min(1, amp));
  };

  EviePresence.prototype.start = function start() {
    if (this.raf || !this.visible) return;
    const self = this;
    const tick = function (now) {
      self.raf = requestAnimationFrame(tick);
      const minDt = self.quality === "high" ? 33 : 70;
      if (now - self.last < minDt) return;
      const dt = Math.min(0.05, (now - (self.last || now)) / 1000);
      self.last = now;
      self.t += dt;
      self.amp += (self.targetAmp - self.amp) * Math.min(1, dt * 8);
      self.draw();
    };
    this.raf = requestAnimationFrame(tick);
  };

  EviePresence.prototype.stopLoop = function stopLoop() {
    if (this.raf) cancelAnimationFrame(this.raf);
    this.raf = 0;
  };

  EviePresence.prototype.resize = function resize() {
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const w = this.canvas.clientWidth || 280;
    const h = this.canvas.clientHeight || 280;
    if (this.canvas.width !== Math.floor(w * dpr) || this.canvas.height !== Math.floor(h * dpr)) {
      this.canvas.width = Math.floor(w * dpr);
      this.canvas.height = Math.floor(h * dpr);
    }
  };

  EviePresence.prototype.draw = function draw() {
    this.resize();
    const ctx = this.ctx;
    const w = this.canvas.width;
    const h = this.canvas.height;
    ctx.clearRect(0, 0, w, h);
    const cx = w * 0.52;
    const cy = h * 0.48;
    const scale = Math.min(w, h);
    const listen = this.state === "listening" ? 1 : 0;
    const think = this.state === "processing" || this.state === "thinking" ? 1 : 0;
    const speak = this.state === "speaking" ? 1 : 0;
    const tool = this.state === "tool" ? 1 : 0;
    const vision = this.state === "camera" || this.state === "vision" ? 1 : 0;
    const body = 0.28 + this.amp * 0.07 + listen * 0.03 + speak * 0.04;
    const motion = this.reduced ? 0 : 1;
    const t = this.t;

    ctx.save();
    ctx.translate(cx, cy);
    if (vision) ctx.scale(1.06, 0.88);
    if (think) ctx.rotate(0.08 * Math.sin(t * 0.6) * motion);

    this._membrane(ctx, scale * (body + 0.06), t * 0.35, this.pearl, 0.22, 1.15, 0.82);
    this._membrane(ctx, scale * body, t * 0.55 + 0.7, this.filament, 0.38 + this.amp * 0.2, 0.92, 1.18);
    this._membrane(ctx, scale * (body * 0.62), t * 0.9 + 1.4, this.ink, 0.08 + speak * 0.05, 1.25, 0.7);

    if (tool) {
      ctx.strokeStyle = "rgba(" + this.filament.join(",") + ",0.45)";
      ctx.lineWidth = Math.max(1.2, scale * 0.006);
      ctx.beginPath();
      ctx.moveTo(scale * 0.08, 0);
      ctx.quadraticCurveTo(scale * 0.28, -scale * 0.12, scale * 0.42, -scale * 0.02);
      ctx.stroke();
    }

    ctx.restore();
  };

  EviePresence.prototype._membrane = function _membrane(ctx, radius, phase, color, alpha, sx, sy) {
    const lobes = 5;
    ctx.save();
    ctx.scale(sx, sy);
    ctx.beginPath();
    for (let i = 0; i <= 72; i += 1) {
      const a = (i / 72) * Math.PI * 2;
      const fold = 0.78 + 0.22 * Math.sin(a * 2 + phase) + 0.08 * Math.sin(a * 3 - phase * 1.3);
      const r = radius * fold;
      const x = Math.cos(a + 0.18) * r * (1.05 + 0.08 * Math.sin(phase + a));
      const y = Math.sin(a - 0.31) * r * (0.92 + 0.1 * Math.cos(phase * 0.7));
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.closePath();
    ctx.fillStyle = "rgba(" + color.join(",") + "," + alpha + ")";
    ctx.fill();
    ctx.restore();
    void lobes;
  };

  root.EviePresence = EviePresence;
  root.EvieOrb = EviePresence;
})(typeof window !== "undefined" ? window : globalThis);
