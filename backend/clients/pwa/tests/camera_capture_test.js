// Camera capture truth: media capability detection and honest burst capture.
// Run: node --test backend/clients/pwa/tests/camera_capture_test.js
// Isolated production functions from app.js: no browser, camera, or network.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const source = fs.readFileSync(path.join(__dirname, "../app.js"), "utf8");

function extract(name) {
  const patterns = [
    new RegExp("^(?:async )?function " + name + "\\b", "m"),
    new RegExp("^const " + name + "\\b", "m"),
    new RegExp("^let " + name + "\\b", "m"),
  ];
  let start = -1;
  for (const pattern of patterns) {
    const match = source.match(pattern);
    if (match) {
      start = match.index;
      break;
    }
  }
  assert.ok(start >= 0, "Production definition exists: " + name);
  const header = source.slice(start, start + 200);
  let bodyStart;
  if (/^const \w+ = \[/.test(header)) {
    bodyStart = start + header.indexOf("[");
  } else if (/^(?:async )?function \w+/.test(header)) {
    const parenOpen = start + header.indexOf("(");
    let depth = 0;
    for (let i = parenOpen; i < source.length; i += 1) {
      if (source[i] === "(") depth += 1;
      else if (source[i] === ")") {
        depth -= 1;
        if (depth === 0) {
          bodyStart = source.indexOf("{", i);
          break;
        }
      }
    }
  } else {
    const lineEnd = source.indexOf("\n", start);
    return source.slice(start, lineEnd > start ? lineEnd : source.length);
  }
  assert.ok(Number.isInteger(bodyStart) && bodyStart > start, "Definition body exists: " + name);
  let depth = 0;
  let quote = null;
  for (let i = bodyStart; i < source.length; i += 1) {
    const ch = source[i];
    if (quote) {
      if (ch === "\\") i += 1;
      else if (ch === quote) quote = null;
      continue;
    }
    if (ch === "'" || ch === '"' || ch === "`") {
      quote = ch;
      continue;
    }
    if (ch === "(" || ch === "{" || ch === "[") depth += 1;
    else if (ch === ")" || ch === "}" || ch === "]") {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error("Unbalanced definition: " + name);
}

function build(extra = {}) {
  const sandbox = Object.assign(
    {
      window: {},
      state: { cameraRole: "pro" },
      console,
    },
    extra
  );
  vm.createContext(sandbox);
  vm.runInContext(
    [
      "CLIP_MIME_CANDIDATES",
      "CLIP_CAPTURE_SECONDS",
      "BURST_FRAMES",
      "BURST_GAP_MS",
      "preferredClipMime",
      "videoRecordingSupported",
      "cameraHardware",
      "isRecordAction",
      "cameraCopyFor",
    ]
      .map(extract)
      .join("\n"),
    sandbox
  );
  return sandbox;
}

test("no MediaRecorder support means video is reported false and burst copy is honest", () => {
  const sandbox = build({ window: {} });
  assert.equal(sandbox.videoRecordingSupported(), false);
  assert.equal(sandbox.preferredClipMime(), null);
  const hardware = sandbox.cameraHardware();
  assert.equal(hardware.media.still, true);
  assert.equal(hardware.media.burst, true);
  assert.equal(hardware.media.video, false);
  assert.equal(hardware.media.clip_mime, undefined);
  assert.match(sandbox.cameraCopyFor("record_clip"), /cannot record video/);
});

test("supported MediaRecorder reports the exact mime and clip capability", () => {
  const sandbox = build({
    window: {
      MediaRecorder: {
        isTypeSupported: (mime) => mime.indexOf("video/mp4") === 0,
      },
    },
  });
  assert.equal(sandbox.videoRecordingSupported(), true);
  assert.equal(sandbox.preferredClipMime(), "video/mp4;codecs=h264");
  const hardware = sandbox.cameraHardware();
  assert.equal(hardware.media.video, true);
  assert.equal(hardware.media.clip_mime, "video/mp4;codecs=h264");
  assert.equal(sandbox.cameraCopyFor("record"), "Recording a short clip on this phone");
});

test("camera hardware keeps the owner-declared quality ranking", () => {
  const pro = build({ state: { cameraRole: "pro" } }).cameraHardware();
  assert.equal(pro.camera_quality, "pro");
  assert.equal(pro.camera_preference_rank, 0);
  const se = build({ state: { cameraRole: "standard" } }).cameraHardware();
  assert.equal(se.camera_quality, "standard");
  assert.equal(se.camera_preference_rank, 10);
  const unknown = build({ state: {} }).cameraHardware();
  assert.equal(unknown.camera_quality, "unknown");
  assert.equal(unknown.camera_preference_rank, 50);
});

test("record actions are recognised in every spelling the server can send", () => {
  const sandbox = build({ window: {} });
  for (const action of ["record", "record_clip", "record_video", "RECORD_CLIP"]) {
    assert.equal(sandbox.isRecordAction(action), true, action);
  }
  for (const action of ["look_once", "observe", "capture_photo", ""]) {
    assert.equal(sandbox.isRecordAction(action), false, action);
  }
});

test("burst capture posts one timestamped sequence and never claims a clip", async () => {
  const posts = [];
  const sandbox = build({ window: {} });
  vm.runInContext(extract("postCameraFrames"), sandbox);
  sandbox.api = async (path, opts) => {
    posts.push({ path, body: JSON.parse(opts.body) });
    return { ok: true, persisted_to_memory_os: true, moments: [{ t_start: 0 }] };
  };
  const receipt = await sandbox.postCameraFrames({
    requestId: "req-1",
    images: ["a", "b", "c", "d"],
    action: "record_clip",
    mediaKind: "burst",
    hasClip: false,
  });
  assert.equal(posts.length, 1);
  assert.equal(posts[0].path, "/v1/device-gateway/camera/result");
  const body = posts[0].body;
  assert.equal(body.last, true);
  assert.equal(body.media_kind, "burst");
  assert.equal(body.has_clip, false);
  assert.equal(body.clip_supported, false);
  assert.equal(body.jpeg_b64, "d");
  assert.deepEqual(
    body.frames.map((frame) => frame.captured_at_ms),
    [0, 1333, 2667, 4000]
  );
  assert.deepEqual(
    body.frames.map((frame) => frame.sequence),
    [0, 1, 2, 3]
  );
  assert.deepEqual(
    body.frames.map((frame) => frame.jpeg_b64),
    ["a", "b", "c", "d"]
  );
  assert.equal(receipt.ok, true);
});

test("a single still posts one frame array without a timeline", async () => {
  const posts = [];
  const sandbox = build({ window: {} });
  vm.runInContext(extract("postCameraFrames"), sandbox);
  sandbox.api = async (path, opts) => {
    posts.push({ path, body: JSON.parse(opts.body) });
    return { ok: true };
  };
  await sandbox.postCameraFrames({
    requestId: "req-2",
    images: ["only"],
    action: "look_once",
    mediaKind: "frame",
    hasClip: false,
  });
  assert.equal(posts.length, 1);
  assert.equal(posts[0].body.frames.length, 1);
  assert.equal(posts[0].body.frames[0].captured_at_ms, 0);
});

test("multipart clip uploads never force a JSON content type", async () => {
  const calls = [];
  const sandbox = build({
    state: { deviceToken: "device-token", accessToken: null },
    AbortController,
    setTimeout,
    clearTimeout,
    fetch: async (path, opts) => {
      calls.push({ path, opts });
      return {
        ok: true,
        status: 200,
        json: async () => ({ ok: true }),
        headers: { get: () => null },
      };
    },
    FormData,
    Blob,
  });
  vm.runInContext(extract("api"), sandbox);
  const form = new FormData();
  form.append("file", new Blob([new Uint8Array([1, 2, 3])]), "clip.mp4");
  await sandbox.api("/v1/vision/clip", { method: "POST", body: form, headers: {} });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, "/v1/vision/clip");
  assert.equal(calls[0].opts.headers["content-type"], undefined);
  assert.equal(calls[0].opts.body, form);
  assert.equal(calls[0].opts.headers.Authorization, "Bearer device-token");
});
