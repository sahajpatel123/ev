// Evie orb — ORIGINAL procedural ring shader (clean-room, no external assets).
//
// A smoky ring of light: domain-warped fractal noise sampled in polar
// coordinates around an analytic ring SDF, wisps drifting tangentially,
// soft additive-style glow, dark core, premultiplied-alpha output so the
// frame composites over any wallpaper. Reference GIF was mood only.
#include <metal_stdlib>
using namespace metal;

// Must match OrbUniforms in OrbRenderer.swift field-for-field (tint first
// so the 16-byte alignment agrees on both sides; the tail is all floats).
struct OrbUniforms {
    float4 tint;          // wisp color (rgb); w unused
    float2 resolution;    // pixels
    float time;           // seconds (frozen by host when static)
    float energy;         // 0..1 smoothed audio envelope
    float radius;         // ring radius, fraction of view half-size
    float thickness;      // ring half-width, fraction of half-size
    float turbulence;     // domain-warp strength 0..1
    float flowSpeed;      // tangential drift rate
    float rotation;       // accumulated ring rotation, radians
    float glow;           // soft halo strength
    float stateBlend;     // secondary pattern morph 0..1
    float halo;           // light-mode dark disc 0..1
    float opacity;        // master opacity
    float orbitStrength;  // acting highlight 0..1
    float orbitAngle;     // highlight position, radians
    float bloom;          // success flash 0..1
    float coolDim;        // error dim 0..1
    float confirmPulse;   // needs_confirm pulse 0..1
    float octaveCount;    // noise octaves 1..4 (perf lever)
    float reduceMotion;   // 0/1 — freeze time in-shader
};

struct VertexOut {
    float4 position [[position]];
};

float orbHash(float2 p) {
    float3 p3 = fract(float3(p.xyx) * 0.1031);
    p3 += dot(p3, p3.yzx + 33.33);
    return fract((p3.x + p3.y) * p3.z);
}

float orbNoise(float2 p) {
    float2 i = floor(p);
    float2 f = fract(p);
    float2 u = f * f * (3.0 - 2.0 * f);
    float a = orbHash(i);
    float b = orbHash(i + float2(1.0, 0.0));
    float c = orbHash(i + float2(0.0, 1.0));
    float d = orbHash(i + float2(1.0, 1.0));
    return mix(mix(a, b, u.x), mix(c, d, u.x), u.y);
}

float orbFbm(float2 p, float octaves) {
    float v = 0.0;
    float amp = 0.5;
    float2 q = p;
    for (int i = 0; i < 4; i++) {
        if (float(i) >= octaves) { break; }
        v += amp * orbNoise(q);
        q = q * 2.03 + float2(17.3, 9.1);
        amp *= 0.5;
    }
    return v;
}

vertex VertexOut orbVertex(uint vid [[vertex_id]]) {
    float2 pos[3] = {
        float2(-1.0, -1.0),
        float2( 3.0, -1.0),
        float2(-1.0,  3.0)
    };
    VertexOut out;
    out.position = float4(pos[vid], 0.0, 1.0);
    return out;
}

fragment float4 orbFragment(VertexOut in [[stage_in]],
                            constant OrbUniforms &u [[buffer(0)]]) {
    float t = mix(u.time, 0.0, clamp(u.reduceMotion, 0.0, 1.0));
    float2 frag = float2(in.position.x, in.position.y);
    float minRes = min(u.resolution.x, u.resolution.y);
    float2 p = (frag - 0.5 * u.resolution) / (0.5 * minRes);
    float r = length(p);
    float theta = atan2(p.y, p.x);

    // Tangential flow: the pattern drifts around the ring, faster with energy.
    float drift = t * u.flowSpeed * (0.55 + 0.45 * u.energy);
    float th = theta - u.rotation - drift;

    // Polar sampling disc plus domain warp for smoke-like wisps.
    float rr = 1.4 + r * 2.2;
    float2 np = float2(cos(th), sin(th)) * rr;
    float warp = orbFbm(np * 1.6 + float2(0.0, -t * u.flowSpeed * 0.4), u.octaveCount);
    float2 wpos = np * 2.6 + (warp - 0.5) * (1.2 + u.turbulence * 3.2)
                + float2(t * u.flowSpeed * 0.15, -t * u.flowSpeed * 0.1);
    float dens = orbFbm(wpos, u.octaveCount);
    dens = mix(dens, 1.0 - dens, u.stateBlend * 0.25);

    // Ring band around the SDF.
    float d = abs(r - u.radius) / max(u.thickness, 1e-3);
    float band = exp(-d * d * 1.6);

    // Wisps: ridged density gated by the band; energy opens the gate.
    float wisp = smoothstep(0.30, 0.85, dens + u.energy * 0.18);
    float ring = band * (0.30 + 0.70 * wisp);

    // Soft glow around the band.
    float glowField = exp(-d * d * 0.35) * u.glow;

    // Acting: orbiting highlight.
    float ad = atan2(sin(theta - u.orbitAngle), cos(theta - u.orbitAngle));
    float orbit = exp(-ad * ad * 20.0) * band * u.orbitStrength;

    // Calm 1 Hz confirm pulse.
    float pulse = u.confirmPulse * (0.5 + 0.5 * sin(t * 6.2831853));

    float bright = ring * (1.0 + u.energy * 0.65)
                 + glowField * 0.40
                 + orbit * 1.3
                 + u.bloom * band * 1.6;
    bright *= 1.0 + pulse * 0.28;
    bright *= 1.0 - u.coolDim * 0.55;

    // Dark core: suppress light well inside the ring.
    float coreMask = smoothstep(u.radius - u.thickness * 2.5,
                                u.radius - u.thickness * 0.5, r);
    float inner = mix(0.10, 1.0, coreMask);

    // Light-mode halo: faint dark disc plus a dark groove directly under the
    // ring band, so white wisps keep local contrast on white wallpapers.
    // (halo is 0 in dark mode, reducing this to the plain wisp output.)
    float haloDisc = (1.0 - smoothstep(u.radius + u.thickness * 4.0,
                                       u.radius + u.thickness * 10.0, r)) * u.halo;
    float groove = exp(-d * d * 0.8) * u.halo;

    float wispA = clamp(bright * inner * 0.9, 0.0, 1.0);
    float a = clamp(max(wispA, groove * 0.6 + haloDisc * 0.45), 0.0, 1.0) * u.opacity;
    float3 wispCol = u.tint.rgb * bright * inner;
    float3 darkCol = float3(0.02, 0.02, 0.03);
    float3 col = mix(darkCol, wispCol, clamp(wispA * 1.4, 0.0, 1.0));
    col = mix(darkCol * haloDisc * 2.5, col, clamp(max(wispA, groove) * 2.0, 0.0, 1.0));
    return float4(col * a, a);
}
