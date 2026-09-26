//
//  NoadcastTests.swift
//  NoadcastTests
//
//  Created by Isaac Khor on 2026.05.15.
//

import Testing
import Foundation
import SwiftData
@testable import Noadcast

struct NoadcastTests {

    @Test @MainActor func podcastDefaultsToAnalysisEnabled() async throws {
        let podcast = Podcast(
            serverID: 1,
            feedURL: try #require(URL(string: "https://example.com/feed.xml")),
            title: "Example"
        )

        #expect(podcast.autoDownloadEnabled)
        #expect(podcast.adAnalysisEnabled)
        #expect(podcast.autoProcessEnabled)
        #expect(podcast.customPlaybackSpeed == nil)
    }

    @Test @MainActor func globalAdAnalysisMirrorDefaultsOn() async throws {
        // Mirror of the server's global switch; the server default is on.
        let settings = AppSettings()

        #expect(settings.adAnalysisEnabled)
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

    @Test func detectedOutroExtendsToEpisodeEndButAdDoesNot() throws {
        let outro = DetectedAd(
            startSeconds: 80,
            endSeconds: 90,
            summary: "Farewell",
            kind: .outro
        )
        let ad = DetectedAd(
            startSeconds: 80,
            endSeconds: 90,
            summary: "Sponsor",
            kind: .ad
        )

        #expect(try #require(outro.sanitized(episodeDuration: 100)).endSeconds == 100)
        #expect(try #require(ad.sanitized(episodeDuration: 100)).endSeconds == 90)
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

    @Test @MainActor func queueDismissalMarksPlayedAndRemovesQueueItem() throws {
        // Dismissing as played records a retention release in UserDefaults
        // (via SyncService.shared); never leave it behind for the host app.
        defer { PendingReleaseStore.clear() }

        let container = try makeTestContainer()
        let context = container.mainContext
        let podcast = Podcast(
            serverID: 9_100_001,
            feedURL: try #require(URL(string: "https://example.com/feed.xml")),
            title: "Queue dismissal podcast"
        )
        context.insert(podcast)
        let episode = Episode(
            serverID: 9_100_002,
            podcastServerID: podcast.serverID,
            guid: "queue-dismissal-test",
            title: "Queue dismissal",
            enclosureURL: try #require(URL(string: "https://example.com/audio.mp3")),
            podcast: podcast
        )
        episode.playbackPosition = 300
        context.insert(episode)
        context.insert(QueueItem(position: 0, episode: episode))
        try context.save()

        SubscriptionService.shared.deleteEpisodeContent(
            episode,
            in: context,
            markAsPlayed: true
        )

        #expect(episode.isPlayed)
        #expect(episode.datePlayed != nil)
        #expect(episode.playbackPosition == 0)
        #expect(try context.fetchCount(FetchDescriptor<QueueItem>()) == 0)
        // The retention release (`DELETE …/audio?reason=played`) is queued.
        #expect(PendingReleaseStore.load().ids.contains(episode.serverID))
    }

    @Test @MainActor func transcriptionProgressShowsAudioTime() throws {
        let transcribing = ActiveJobDTO(
            episodeId: 1,
            state: .transcribing,
            stage: .transcribe,
            current: 65,
            total: 90
        )
        #expect(TimeFormatting.progressDetail(for: transcribing) == "1:05 / 1:30")

        // `download` reports bytes.
        let downloading = ActiveJobDTO(
            episodeId: 2,
            state: .downloading,
            stage: .download,
            current: 12_300_000,
            total: 50_000_000
        )
        let bytes = try #require(TimeFormatting.progressDetail(for: downloading))
        #expect(bytes.contains(" / "))

        // `classify` is indeterminate.
        let classifying = ActiveJobDTO(episodeId: 3, state: .classifying, stage: .classify)
        #expect(TimeFormatting.progressDetail(for: classifying) == nil)
    }

}
