import EvieOrb
import Foundation

/// R4: frame-pacing policy — fps per state, static caching, visibility gating.
func runPacingChecks(_ c: inout CheckContext) {
    // Fps table.
    c.expect(FramePacingPolicy.targetFPS(for: .listening, lowPowerMode: false) == 60, "listening fps")
    c.expect(FramePacingPolicy.targetFPS(for: .speaking, lowPowerMode: false) == 60, "speaking fps")
    c.expect(FramePacingPolicy.targetFPS(for: .thinking, lowPowerMode: false) == 30, "thinking fps")
    c.expect(FramePacingPolicy.targetFPS(for: .acting, lowPowerMode: false) == 30, "acting fps")
    c.expect(FramePacingPolicy.targetFPS(for: .needsConfirm, lowPowerMode: false) == 30, "confirm fps")
    c.expect(FramePacingPolicy.targetFPS(for: .success, lowPowerMode: false) == 30, "success fps")
    c.expect(FramePacingPolicy.targetFPS(for: .error, lowPowerMode: false) == 30, "error fps")
    c.expect(FramePacingPolicy.targetFPS(for: .idle, lowPowerMode: false) == 0, "idle must be 0 fps")
    c.expect(FramePacingPolicy.targetFPS(for: .offline, lowPowerMode: false) == 0, "offline must be 0 fps")

    // Low Power Mode caps everything animated at 15 fps, static stays 0.
    for state in OrbState.allCases {
        let fps = FramePacingPolicy.targetFPS(for: state, lowPowerMode: true)
        if FramePacingPolicy.isStaticState(state) {
            c.expect(fps == 0, "\(state) must stay 0 fps in Low Power Mode")
        } else {
            c.expect(fps == 15, "\(state) must be 15 fps in Low Power Mode, got \(fps)")
        }
    }

    // Static + settled + quiet => skip (cached frame stays on screen).
    c.expect(
        FramePacingPolicy.shouldRender(
            state: .idle, visible: true, paramsChanged: false, levelActive: false
        ) == false,
        "idle must not render when settled"
    )
    c.expect(
        FramePacingPolicy.shouldRender(
            state: .offline, visible: true, paramsChanged: false, levelActive: false
        ) == false,
        "offline must not render when settled"
    )

    // A param change forces a frame even in a static state (transitions).
    c.expect(
        FramePacingPolicy.shouldRender(
            state: .idle, visible: true, paramsChanged: true, levelActive: false
        ) == true,
        "settling transition must render"
    )

    // Animated states always render while visible.
    c.expect(
        FramePacingPolicy.shouldRender(
            state: .listening, visible: true, paramsChanged: false, levelActive: false
        ) == true,
        "listening must render while visible"
    )

    // Invisible (hidden/occluded/screen asleep) => never render.
    for state in OrbState.allCases {
        c.expect(
            FramePacingPolicy.shouldRender(
                state: state, visible: false, paramsChanged: true, levelActive: true
            ) == false,
            "\(state) rendered while invisible"
        )
    }

    // Link management: static + settled + quiet => link stopped (0 frames, ~0% CPU).
    c.expect(
        FramePacingPolicy.linkShouldRun(state: .idle, settled: true, levelActive: false, visible: true) == false,
        "idle link should stop when settled"
    )
    c.expect(
        FramePacingPolicy.linkShouldRun(state: .offline, settled: true, levelActive: false, visible: true) == false,
        "offline link should stop when settled"
    )
    c.expect(
        FramePacingPolicy.linkShouldRun(state: .idle, settled: false, levelActive: false, visible: true) == true,
        "settling transition needs the link"
    )
    c.expect(
        FramePacingPolicy.linkShouldRun(state: .listening, settled: true, levelActive: true, visible: true) == true,
        "listening needs the link"
    )
    c.expect(
        FramePacingPolicy.linkShouldRun(state: .listening, settled: true, levelActive: true, visible: false) == false,
        "invisible must stop the link"
    )

    // Pacer smoke: ticks without any window, stops cleanly.
    let pacer = FramePacer()
    var ticks = 0
    pacer.onTick = { _ in ticks += 1 }
    pacer.targetFPS = 60
    pacer.isVisible = true
    pacer.start()
    Thread.sleep(forTimeInterval: 0.35)
    pacer.stop()
    c.expect(ticks > 0, "display link produced no ticks headless")
    // FPS thinning: at 30 fps target on a 60 Hz display, ~half the ticks render.
    let pacer2 = FramePacer()
    var offered = 0
    pacer2.onTick = { _ in offered += 1 }
    pacer2.targetFPS = 15
    pacer2.isVisible = true
    pacer2.start()
    Thread.sleep(forTimeInterval: 0.4)
    pacer2.stop()
    c.expect(offered > 0 && offered <= 12, "15 fps thinning gave \(offered) ticks in 0.4 s")
}
