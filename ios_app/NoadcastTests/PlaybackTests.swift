//
//  PlaybackTests.swift
//  NoadcastTests
//
//  The pure playback decisions: where audio comes from (`PlaybackSourceResolver`),
//  what auto-advance picks, and the ad-skip loop guard (`AdSkipPlanner`).
//

import Testing
import Foundation
@testable import Noadcast

struct PlaybackSourceResolverTests {

    private static let fileURL = URL(fileURLWithPath: "/tmp/noadcast-tests/episode.mp3")

    private func inputs(
        local: URL? = nil,
        configured: Bool = true,
        online: Bool = true,
        wifi: Bool = true,
        audio: ServerAudioState = .present,
        policy: StreamingPolicy = .anyNetwork
    ) -> PlaybackSourceInputs {
        PlaybackSourceInputs(
            localFileURL: local,
            isServerConfigured: configured,
            isOnline: online,
            isWiFi: wifi,
            audioState: audio,
            streamingPolicy: policy
        )
    }

    @Test func localFileWinsOverEverything() {
        let url = Self.fileURL
        for audio in ServerAudioState.allCases {
            for policy in StreamingPolicy.allCases {
                for online in [true, false] {
                    let source = PlaybackSourceResolver.resolve(
                        inputs(local: url, configured: false, online: online, wifi: false, audio: audio, policy: policy)
                    )
                    #expect(source == .localFile(url))
                }
            }
        }
    }

    @Test func resolverDecisionTable() {
        // Configuration first, then reachability, then policy, then the server's copy.
        #expect(PlaybackSourceResolver.resolve(inputs(configured: false)) == .unavailable(.notConfigured))
        #expect(PlaybackSourceResolver.resolve(inputs(configured: false, online: false)) == .unavailable(.notConfigured))
        #expect(PlaybackSourceResolver.resolve(inputs(online: false)) == .unavailable(.offline))
        #expect(PlaybackSourceResolver.resolve(inputs(online: false, policy: .never)) == .unavailable(.offline))
        #expect(PlaybackSourceResolver.resolve(inputs(policy: .never)) == .unavailable(.streamingDisabled))
        #expect(PlaybackSourceResolver.resolve(inputs(audio: .absent, policy: .never)) == .unavailable(.streamingDisabled))
        #expect(PlaybackSourceResolver.resolve(inputs(wifi: false, policy: .wifiOnly)) == .unavailable(.streamingRequiresWiFi))
        #expect(PlaybackSourceResolver.resolve(inputs(wifi: true, policy: .wifiOnly)) == .stream)
        #expect(PlaybackSourceResolver.resolve(inputs(wifi: false, policy: .anyNetwork)) == .stream)
        for audio in [ServerAudioState.absent, .evicted, .partial, .unknown] {
            #expect(PlaybackSourceResolver.resolve(inputs(audio: audio)) == .unavailable(.audioNotOnServer), "\(audio)")
        }
        #expect(PlaybackSourceResolver.resolve(inputs(audio: .present)) == .stream)
    }

    @Test func playabilityFlagsSeparatePreparingFromBlocked() {
        let local = PlaybackSource.localFile(Self.fileURL)
        #expect(local.isPlayableNow)
        #expect(local.canStartPlayback)
        #expect(PlaybackSource.stream.isPlayableNow)
        #expect(PlaybackSource.stream.canStartPlayback)

        // Not on the server yet: can start (the player shows "Preparing…"),
        // but is not playable right now.
        let preparing = PlaybackSource.unavailable(.audioNotOnServer)
        #expect(!preparing.isPlayableNow)
        #expect(preparing.canStartPlayback)

        let blocked: [PlaybackUnavailableReason] = [.notConfigured, .offline, .streamingDisabled, .streamingRequiresWiFi]
        for reason in blocked {
            let source = PlaybackSource.unavailable(reason)
            #expect(!source.isPlayableNow, "\(reason)")
            #expect(!source.canStartPlayback, "\(reason)")
        }
    }

    @Test func autoAdvancePicksTheFirstPlayableItemInQueueOrder() {
        let local = PlaybackSource.localFile(Self.fileURL)

        // Queue order wins over "is downloaded".
        #expect(PlaybackSourceResolver.firstPlayableIndex([.unavailable(.offline), .stream, local]) == 1)
        #expect(PlaybackSourceResolver.firstPlayableIndex([.stream, local]) == 0)
        // "Preparing on the server" is passed over, not waited on.
        #expect(PlaybackSourceResolver.firstPlayableIndex([.unavailable(.audioNotOnServer), local, .stream]) == 1)
        // Nothing playable.
        #expect(PlaybackSourceResolver.firstPlayableIndex([
            .unavailable(.offline),
            .unavailable(.audioNotOnServer),
            .unavailable(.streamingRequiresWiFi),
        ]) == nil)
        #expect(PlaybackSourceResolver.firstPlayableIndex([]) == nil)
    }

}

struct AdSkipPlannerTests {

    private static let episodeDuration: Double = 3_600

    private func seekTarget(_ decision: AdSkipPlanner.Decision) -> Double? {
        if case .seek(let target, _, _) = decision {
            return target
        }
        return nil
    }

    @Test func seekThatLandsShortIsRetriedAtMostThreeTimes() throws {
        #expect(AdSkipPlanner.maxAttemptsPerRegion == 3)
        var planner = AdSkipPlanner()
        let region = AdRegion(startSeconds: 10, endSeconds: 60, kind: .ad)
        let regions = [region]
        func decide(at time: Double, now: TimeInterval) -> AdSkipPlanner.Decision {
            planner.decide(
                currentTime: time,
                duration: Self.episodeDuration,
                regions: regions,
                chainSkipGapSeconds: 5,
                skipsAds: true,
                skipsIntrosAndOutros: true,
                now: now
            )
        }
        let landing = 60 + AdSkipPlanner.landingOffset

        // Attempt 1.
        let first = decide(at: 12, now: 0)
        #expect(first == .seek(target: landing, regionsSkipped: 1, savedSeconds: 48))
        let firstTarget = try #require(seekTarget(first))
        #expect(abs(firstTarget - 60.05) < 1e-9)
        #expect(planner.pendingSkipTarget == landing)
        #expect(planner.attemptCount(for: region) == 1)

        // A tick before the seek completes must not re-enter the region.
        let whilePending = decide(at: 12.25, now: 0.25)
        #expect(whilePending == AdSkipPlanner.Decision.none)
        planner.skipSeekCompleted()
        #expect(planner.pendingSkipTarget == nil)

        // Attempt 2: the seek landed short (VBR stream), still inside the region.
        let second = decide(at: 59, now: 1)
        #expect(seekTarget(second) == landing)
        planner.skipSeekCompleted()

        // Attempt 3.
        let third = decide(at: 59, now: 2)
        #expect(seekTarget(third) == landing)
        #expect(planner.attemptCount(for: region) == AdSkipPlanner.maxAttemptsPerRegion)
        planner.skipSeekCompleted()

        // Cap reached: the region is left to play instead of looping.
        let capped = decide(at: 59, now: 3)
        #expect(capped == AdSkipPlanner.Decision.none)
        #expect(planner.pendingSkipTarget == nil)
        let stillCapped = decide(at: 59.5, now: 4)
        #expect(stillCapped == AdSkipPlanner.Decision.none)

        // A new item / user seek / new markers re-enables the region.
        planner.reset()
        #expect(planner.attemptCount(for: region) == 0)
        let afterReset = decide(at: 59, now: 5)
        #expect(seekTarget(afterReset) == landing)
    }

    @Test func chainSkipJumpsOverRegionsWithinTheGap() {
        let regions = [
            AdRegion(startSeconds: 100, endSeconds: 160, kind: .ad),
            AdRegion(startSeconds: 163, endSeconds: 200, kind: .ad),
            AdRegion(startSeconds: 400, endSeconds: 430, kind: .ad),
        ]

        var chained = AdSkipPlanner()
        let decision = chained.decide(
            currentTime: 101,
            duration: Self.episodeDuration,
            regions: regions,
            chainSkipGapSeconds: 5,
            skipsAds: true,
            skipsIntrosAndOutros: true,
            now: 0
        )
        #expect(decision == .seek(target: 200 + AdSkipPlanner.landingOffset, regionsSkipped: 2, savedSeconds: 99))

        // With chaining off only the current region is skipped.
        var unchained = AdSkipPlanner()
        let single = unchained.decide(
            currentTime: 101,
            duration: Self.episodeDuration,
            regions: regions,
            chainSkipGapSeconds: 0,
            skipsAds: true,
            skipsIntrosAndOutros: true,
            now: 0
        )
        #expect(single == .seek(target: 160 + AdSkipPlanner.landingOffset, regionsSkipped: 1, savedSeconds: 59))
    }

    @Test func overlappingAndTouchingAdsAreSkippedInOneSeek() {
        let regions = [
            AdRegion(startSeconds: 10, endSeconds: 30, kind: .ad),
            AdRegion(startSeconds: 20, endSeconds: 40, kind: .ad),
            AdRegion(startSeconds: 40, endSeconds: 60, kind: .ad),
        ]
        var planner = AdSkipPlanner()
        let decision = planner.decide(
            currentTime: 15,
            duration: Self.episodeDuration,
            regions: regions,
            chainSkipGapSeconds: 0,
            skipsAds: true,
            skipsIntrosAndOutros: false,
            now: 0
        )
        #expect(decision == .seek(target: 60 + AdSkipPlanner.landingOffset, regionsSkipped: 3, savedSeconds: 45))
    }

    @Test func disabledOverlappingKindDoesNotHideAnEnabledAd() {
        let regions = [
            AdRegion(startSeconds: 0, endSeconds: 30, kind: .intro),
            AdRegion(startSeconds: 20, endSeconds: 60, kind: .ad),
        ]
        var planner = AdSkipPlanner()
        let decision = planner.decide(
            currentTime: 25,
            duration: Self.episodeDuration,
            regions: regions,
            chainSkipGapSeconds: 0,
            skipsAds: true,
            skipsIntrosAndOutros: false,
            now: 0
        )
        #expect(decision == .seek(target: 60 + AdSkipPlanner.landingOffset, regionsSkipped: 1, savedSeconds: 35))
    }

    @Test func skippingPastTheEndFinishesTheEpisode() {
        var planner = AdSkipPlanner()
        let regions = [AdRegion(startSeconds: 3_500, endSeconds: 3_600, kind: .outro)]
        let decision = planner.decide(
            currentTime: 3_550,
            duration: 3_600,
            regions: regions,
            chainSkipGapSeconds: 5,
            skipsAds: true,
            skipsIntrosAndOutros: true,
            now: 0
        )
        #expect(decision == .finish(regionsSkipped: 1, savedSeconds: 50))
        #expect(planner.pendingSkipTarget == nil)

        // Unknown duration (0): never "finish", just seek past the region.
        var unknownDuration = AdSkipPlanner()
        let seek = unknownDuration.decide(
            currentTime: 12,
            duration: 0,
            regions: [AdRegion(startSeconds: 10, endSeconds: 60, kind: .ad)],
            chainSkipGapSeconds: 5,
            skipsAds: true,
            skipsIntrosAndOutros: true,
            now: 0
        )
        #expect(seek == .seek(target: 60 + AdSkipPlanner.landingOffset, regionsSkipped: 1, savedSeconds: 48))
    }

    @Test func perKindTogglesAreHonoured() {
        let regions = [
            AdRegion(startSeconds: 0, endSeconds: 30, kind: .intro),
            AdRegion(startSeconds: 32, endSeconds: 60, kind: .ad),
            AdRegion(startSeconds: 600, endSeconds: 660, kind: .ad),
        ]
        func decide(_ planner: inout AdSkipPlanner, at time: Double, skipsAds: Bool, skipsIntros: Bool) -> AdSkipPlanner.Decision {
            planner.decide(
                currentTime: time,
                duration: Self.episodeDuration,
                regions: regions,
                chainSkipGapSeconds: 5,
                skipsAds: skipsAds,
                skipsIntrosAndOutros: skipsIntros,
                now: 0
            )
        }

        // Ads off: ads play, the intro is still skipped, and the adjacent ad
        // is not chained onto it.
        var adsOff = AdSkipPlanner()
        let inAd = decide(&adsOff, at: 610, skipsAds: false, skipsIntros: true)
        #expect(inAd == AdSkipPlanner.Decision.none)
        let inIntro = decide(&adsOff, at: 1, skipsAds: false, skipsIntros: true)
        #expect(inIntro == .seek(target: 30 + AdSkipPlanner.landingOffset, regionsSkipped: 1, savedSeconds: 29))

        // Intros/outros off: the intro plays, ads are skipped.
        var introsOff = AdSkipPlanner()
        let introPlays = decide(&introsOff, at: 1, skipsAds: true, skipsIntros: false)
        #expect(introPlays == AdSkipPlanner.Decision.none)
        let adSkipped = decide(&introsOff, at: 610, skipsAds: true, skipsIntros: false)
        #expect(adSkipped == .seek(target: 660 + AdSkipPlanner.landingOffset, regionsSkipped: 1, savedSeconds: 50))

        // Both on: intro + adjacent ad are skipped in one seek.
        var both = AdSkipPlanner()
        let chained = decide(&both, at: 1, skipsAds: true, skipsIntros: true)
        #expect(chained == .seek(target: 60 + AdSkipPlanner.landingOffset, regionsSkipped: 2, savedSeconds: 59))
    }

    @Test func pendingSkipGuardExpiresAfterTheTimeout() {
        var planner = AdSkipPlanner()
        let region = AdRegion(startSeconds: 10, endSeconds: 60, kind: .ad)
        func decide(now: TimeInterval) -> AdSkipPlanner.Decision {
            planner.decide(
                currentTime: 12,
                duration: Self.episodeDuration,
                regions: [region],
                chainSkipGapSeconds: 5,
                skipsAds: true,
                skipsIntrosAndOutros: true,
                now: now
            )
        }
        let start: TimeInterval = 1_000

        let first = decide(now: start)
        #expect(seekTarget(first) != nil)
        let stillPending = decide(now: start + AdSkipPlanner.pendingSkipTimeout - 0.5)
        #expect(stillPending == AdSkipPlanner.Decision.none)
        #expect(planner.pendingSkipTarget != nil)

        // The seek completion never arrived (item replaced mid-seek): the
        // guard gives up instead of disabling skipping forever.
        let afterTimeout = decide(now: start + AdSkipPlanner.pendingSkipTimeout + 0.5)
        #expect(seekTarget(afterTimeout) == 60 + AdSkipPlanner.landingOffset)
        #expect(planner.attemptCount(for: region) == 2)
    }

}
