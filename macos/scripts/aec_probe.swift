#!/usr/bin/env swift
import AVFoundation
import Darwin
import Foundation

/// Isolated macOS voice-processing / AEC experiment.
///
/// Not linked into EV.app. Run:
///   swift macos/scripts/aec_probe.swift
///
/// An earlier accessory-path experiment aborted the process when playback
/// started on a second audio unit. This probe uses one engine only, in a
/// throwaway process, so a crash cannot take down EV.

let engine = AVAudioEngine()
let player = AVAudioPlayerNode()
let input = engine.inputNode

engine.attach(player)
let format = AVAudioFormat(
    commonFormat: .pcmFormatFloat32,
    sampleRate: 48_000,
    channels: 1,
    interleaved: false
)
guard let format else {
    FileHandle.standardError.write(Data("AEC_FAIL format\n".utf8))
    exit(2)
}
engine.connect(player, to: engine.mainMixerNode, format: format)

do {
    try input.setVoiceProcessingEnabled(true)
    FileHandle.standardOutput.write(Data("AEC_ENABLE_OK\n".utf8))
} catch {
    FileHandle.standardOutput.write(Data("AEC_ENABLE_FAIL \(error)\n".utf8))
    exit(3)
}

do {
    engine.prepare()
    try engine.start()
    FileHandle.standardOutput.write(Data("AEC_START_OK\n".utf8))
} catch {
    FileHandle.standardOutput.write(Data("AEC_START_FAIL \(error)\n".utf8))
    exit(4)
}

if let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 4800) {
    buffer.frameLength = 4800
    if let channel = buffer.floatChannelData?[0] {
        for i in 0..<Int(buffer.frameLength) {
            channel[i] = sinf(Float(i) * 0.08) * 0.05
        }
    }
    player.scheduleBuffer(buffer, completionHandler: nil)
    player.play()
    FileHandle.standardOutput.write(Data("AEC_PLAY_OK\n".utf8))
}

Thread.sleep(forTimeInterval: 0.4)
FileHandle.standardOutput.write(Data("AEC_STABLE\n".utf8))
engine.stop()
exit(0)
