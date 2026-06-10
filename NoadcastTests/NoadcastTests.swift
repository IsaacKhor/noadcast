//
//  NoadcastTests.swift
//  NoadcastTests
//
//  Created by Isaac Khor on 2026.05.15.
//

import Testing
import Foundation
@testable import Noadcast

struct NoadcastTests {

    @Test @MainActor func podcastDefaultsToAnalysisEnabled() async throws {
        let podcast = Podcast(
            feedURL: try #require(URL(string: "https://example.com/feed.xml")),
            title: "Example"
        )

        #expect(podcast.autoDownloadEnabled)
        #expect(podcast.aiProcessingEnabled)
    }

    @Test @MainActor func globalAdAnalysisDefaultsOff() async throws {
        let settings = AppSettings()

        #expect(!settings.adAnalysisEnabled)
    }

    @Test @MainActor func adDetectionBackendDefaultsToDirectGemini() async throws {
        let settings = AppSettings()

        #expect(settings.adDetectionBackend == .geminiFiles)
        #expect(settings.adDetectionServerHost == "http://127.0.0.1")
        #expect(settings.adDetectionServerPort == 8765)
    }

    @Test @MainActor func resetPlaybackHistoryClearsListeningTotals() async throws {
        let settings = AppSettings()
        settings.lifetimePlayedSeconds = 120
        settings.lifetimeAdSkipSeconds = 30

        settings.resetPlaybackHistoryStatistics()

        #expect(settings.lifetimePlayedSeconds == 0)
        #expect(settings.lifetimeAdSkipSeconds == 0)
    }

    @Test func detectedAdSanitizerClampsToEpisodeDuration() async throws {
        let ad = DetectedAd(
            startSeconds: 95,
            endSeconds: 120,
            summary: "Post-roll",
            kind: .outro
        )

        let sanitized = try #require(ad.sanitized(episodeDuration: 100))

        #expect(sanitized.startSeconds == 95)
        #expect(sanitized.endSeconds == 100)
    }

    @Test func detectedAdSanitizerDropsSegmentsOutsideEpisode() async throws {
        let ad = DetectedAd(
            startSeconds: 105,
            endSeconds: 120,
            summary: "Impossible marker",
            kind: .ad
        )

        #expect(ad.sanitized(episodeDuration: 100)?.startSeconds == nil)
    }

    @Test func detectedAdSanitizerDropsNonFiniteSegments() async throws {
        let ad = DetectedAd(
            startSeconds: 10,
            endSeconds: .infinity,
            summary: "Impossible marker",
            kind: .ad
        )

        #expect(ad.sanitized(episodeDuration: 100)?.startSeconds == nil)
    }

    @Test func playerSeekClampingAllowsUnknownDuration() async throws {
        #expect(PlayerService.clampedPlaybackTime(42, duration: 0) == 42)
        #expect(PlayerService.clampedPlaybackTime(120, duration: 100) == 100)
        #expect(PlayerService.clampedPlaybackTime(.nan, duration: 100) == 0)
    }

    @Test func whisperServerURLDefaultsToAnalyzeEndpoint() async throws {
        let url = try CloudAdDetectionService.serverAnalyzeURL(
            host: "127.0.0.1",
            port: 8765
        )

        #expect(url.absoluteString == "http://127.0.0.1:8765/analyze")
    }

    @Test func whisperServerURLPreservesBasePath() async throws {
        let url = try CloudAdDetectionService.serverAnalyzeURL(
            host: "http://example.local/noadcast",
            port: 8080
        )

        #expect(url.absoluteString == "http://example.local:8080/noadcast/analyze")
    }

}
