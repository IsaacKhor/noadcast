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
        let suiteName = "NoadcastTests.PendingRelease.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suiteName))
        defer { defaults.removePersistentDomain(forName: suiteName) }
        var cancelledTransfers: [Int] = []
        var cancelledRequests: [Int] = []
        let service = SubscriptionService(
            releasePlayedAudio: { PendingReleaseStore.add($0, instanceId: "test-instance", defaults: defaults) },
            cancelPendingRelease: { PendingReleaseStore.remove($0, defaults: defaults) },
            cancelLocalTransfer: { cancelledTransfers.append($0) },
            cancelServerAudioRequest: { cancelledRequests.append($0) }
        )

        let container = try makeTestContainer()
        let context = container.mainContext
        // Older installations may still have this preference disabled.
        // Marking played must now remove content regardless.
        AppSettings.current(in: context).autoDeleteAfterPlayed = false
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
        let filename = "played-test-\(UUID().uuidString).mp3"
        let fileURL = AudioStorage.fileURL(for: filename)
        try Data([1, 2, 3]).write(to: fileURL)
        defer { try? FileManager.default.removeItem(at: fileURL) }
        episode.localFilename = filename
        episode.fileSizeBytes = 3
        episode.localAudioSha256 = "fixture-hash"
        episode.applyServerState(ServerEpisodeState.transcribing.rawValue)
        episode.setDownloadState(.queued)
        episode.downloadIsUserInitiated = true
        episode.downloadRequestedAt = .now
        episode.downloadProgress = 0.4
        episode.downloadedBytes = 400
        episode.downloadTotalBytes = 1_000
        context.insert(episode)
        context.insert(QueueItem(position: 0, episode: episode))
        try context.save()

        service.deleteEpisodeContent(
            episode,
            in: context,
            markAsPlayed: true
        )

        #expect(episode.isPlayed)
        #expect(episode.datePlayed != nil)
        #expect(episode.playbackPosition == 0)
        #expect(!FileManager.default.fileExists(atPath: fileURL.path))
        #expect(episode.localFilename == nil)
        #expect(episode.fileSizeBytes == nil)
        #expect(episode.localAudioSha256 == nil)
        #expect(cancelledTransfers == [episode.serverID])
        #expect(cancelledRequests == [episode.serverID])
        #expect(episode.downloadState == .idle)
        #expect(!episode.downloadIsUserInitiated)
        #expect(episode.downloadRequestedAt == nil)
        #expect(episode.downloadProgress == 0)
        #expect(episode.downloadedBytes == nil)
        #expect(episode.downloadTotalBytes == nil)
        #expect(try context.fetchCount(FetchDescriptor<QueueItem>()) == 0)
        // The server mirror may still say "transcribing" until the next
        // sync. Played episodes stay out of Status immediately.
        #expect(try context.fetchCount(FetchDescriptor<Episode>(predicate: #Predicate<Episode> {
            (!$0.isPlayed && ($0.isBusy || $0.serverStateRaw == "failed" || $0.downloadStateRaw == "failed")) || $0.localFilename != nil
        })) == 0)
        // The retention release (`DELETE …/audio?reason=played`) is queued.
        #expect(PendingReleaseStore.load(defaults: defaults).ids.contains(episode.serverID))

        // A later explicit queue action reverses the played dismissal and
        // cancels its unsent release without starting a real transfer.
        AppSettings.current(in: context).autoDownloadPolicy = .manualOnly
        try context.save()
        #expect(service.addToQueue(episode, in: context))
        #expect(!episode.isPlayed)
        #expect(PendingReleaseStore.load(defaults: defaults).ids.isEmpty)
        #expect(try context.fetchCount(FetchDescriptor<QueueItem>()) == 1)
    }

    @Test @MainActor func playedContentRepairPreservesHistoryAndIgnoresRevivedEpisodes() throws {
        let container = try makeTestContainer()
        let context = container.mainContext
        var released: [Int] = []
        var cancelled: [Int] = []
        let service = SubscriptionService(
            releasePlayedAudio: { released.append($0) },
            cancelPendingRelease: { _ in },
            cancelLocalTransfer: { cancelled.append($0) },
            cancelServerAudioRequest: { _ in }
        )
        let played = Episode(serverID: 9_100_501, podcastServerID: 1, guid: "repair", title: "Repair")
        played.isPlayed = true
        played.datePlayed = SyncFixtures.now
        played.playbackPosition = 100
        played.setDownloadState(.downloading)
        played.downloadTaskIdentifier = 44
        let filename = "repair-test-\(UUID().uuidString).mp3"
        let fileURL = AudioStorage.fileURL(for: filename)
        try Data([1, 2, 3]).write(to: fileURL)
        defer { try? FileManager.default.removeItem(at: fileURL) }
        played.localFilename = filename
        context.insert(played)
        context.insert(QueueItem(position: 0, episode: played))
        let revived = Episode(serverID: 9_100_502, podcastServerID: 1, guid: "revived", title: "Revived")
        revived.setDownloadState(.queued)
        context.insert(revived)
        context.insert(QueueItem(position: 1, episode: revived))
        try context.save()

        service.cleanUpPlayedContent(episodeServerIDs: [played.serverID, revived.serverID], in: context)

        #expect(played.isPlayed)
        #expect(played.datePlayed == SyncFixtures.now)
        #expect(played.playbackPosition == 100)
        #expect(played.localFilename == nil)
        #expect(played.downloadState == .idle)
        #expect(played.downloadTaskIdentifier == nil)
        #expect(!FileManager.default.fileExists(atPath: fileURL.path))
        #expect(cancelled == [played.serverID])
        #expect(released.isEmpty)
        #expect(revived.downloadState == .queued)
        #expect(try context.fetch(FetchDescriptor<QueueItem>()).map { $0.episode?.serverID } == [revived.serverID])
    }

    @Test @MainActor func playedEpisodeStaysDismissedAfterServerMirrorUpdate() async throws {
        let suiteName = "NoadcastTests.PendingRelease.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suiteName))
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let service = SubscriptionService(
            releasePlayedAudio: { PendingReleaseStore.add($0, instanceId: "test-instance", defaults: defaults) },
            cancelPendingRelease: { PendingReleaseStore.remove($0, defaults: defaults) }
        )
        let container = try makeTestContainer()
        let engine = SyncEngine(modelContainer: container)
        _ = try await engine.apply(
            page: SyncFixtures.page(
                podcasts: [SyncFixtures.podcast(9_100_101)],
                episodes: [SyncFixtures.episode(9_100_102, podcast: 9_100_101, state: .downloading)],
                nextSince: 1
            ), options: SyncFixtures.options, save: true
        )
        let context = container.mainContext
        let episode = try #require(try storedEpisode(9_100_102, in: context))
        service.deleteEpisodeContent(episode, in: context, markAsPlayed: true)

        _ = try await engine.apply(
            page: SyncFixtures.page(
                episodes: [SyncFixtures.episode(9_100_102, podcast: 9_100_101, state: .classifying)],
                nextSince: 2
            ), options: SyncFixtures.options, save: true
        )
        let refreshed = try #require(try storedEpisode(9_100_102, in: ModelContext(container)))
        #expect(refreshed.isPlayed)
        #expect(refreshed.downloadState == .idle)
        #expect(refreshed.serverState == .classifying)
        #expect(PendingReleaseStore.load(defaults: defaults).ids.contains(refreshed.serverID))
    }

    @Test @MainActor func statusShowsLegacyPlayedAudioUntilCleanupButHidesPlayedWork() throws {
        let container = try makeTestContainer()
        let context = container.mainContext
        let podcast = Podcast(
            serverID: 9_100_201,
            feedURL: try #require(URL(string: "https://example.com/status.xml")),
            title: "Status fixture"
        )
        context.insert(podcast)
        // Old stores can still contain played audio from before cleanup was
        // unconditional. Keep it visible until reconciliation removes it.
        let legacyAudio = Episode(serverID: 9_100_202, podcastServerID: podcast.serverID, guid: "legacy-audio", title: "Legacy audio", podcast: podcast)
        legacyAudio.isPlayed = true
        legacyAudio.localFilename = "legacy-audio-fixture.mp3"
        legacyAudio.fileSizeBytes = 1_000
        legacyAudio.applyServerState(ServerEpisodeState.classifying.rawValue)
        context.insert(legacyAudio)
        let dismissed = Episode(serverID: 9_100_203, podcastServerID: podcast.serverID, guid: "dismissed", title: "Dismissed", podcast: podcast)
        dismissed.isPlayed = true
        dismissed.applyServerState(ServerEpisodeState.classifying.rawValue)
        context.insert(dismissed)
        try context.save()

        let visible = try context.fetch(FetchDescriptor<Episode>(predicate: #Predicate<Episode> {
            (!$0.isPlayed && ($0.isBusy || $0.serverStateRaw == "failed" || $0.downloadStateRaw == "failed")) || $0.localFilename != nil
        }))
        #expect(visible.map(\.serverID) == [legacyAudio.serverID])
    }

    @Test @MainActor func lateDownloadProgressCannotWriteIntoReplacementTransfer() async throws {
        let container = try makeTestContainer()
        let context = container.mainContext
        let podcast = Podcast(
            serverID: 9_100_301,
            feedURL: try #require(URL(string: "https://example.com/progress.xml")),
            title: "Progress fixture"
        )
        context.insert(podcast)
        let episode = Episode(serverID: 9_100_302, podcastServerID: podcast.serverID, guid: "progress", title: "Progress", podcast: podcast)
        episode.setDownloadState(.downloading)
        episode.downloadTaskIdentifier = 202
        context.insert(episode)
        try context.save()

        let writer = DownloadProgressWriter(modelContainer: container)
        await writer.record(serverID: episode.serverID, taskID: 101, written: 500, expected: 1_000)
        #expect(try storedEpisode(episode.serverID, in: ModelContext(container))?.downloadProgress == 0)
        await writer.record(serverID: episode.serverID, taskID: 202, written: 500, expected: 1_000)
        #expect(try storedEpisode(episode.serverID, in: ModelContext(container))?.downloadProgress == 0.5)
    }

    @Test @MainActor func oldVersionDownloadCompletionIsAcceptedOnlyBeforeReplacementOrDismissal() {
        #expect(DownloadManager.acceptsLegacyCompletion(
            wasDownloadingAtStartup: true, isPlayed: false, state: .downloading
        ))
        #expect(DownloadManager.acceptsLegacyCompletion(
            wasDownloadingAtStartup: true, isPlayed: false, state: .queued
        ))
        #expect(!DownloadManager.acceptsLegacyCompletion(
            wasDownloadingAtStartup: true, isPlayed: true, state: .downloading
        ))
        #expect(!DownloadManager.acceptsLegacyCompletion(
            wasDownloadingAtStartup: true, isPlayed: false, state: .idle
        ))
        #expect(!DownloadManager.acceptsLegacyCompletion(
            wasDownloadingAtStartup: false, isPlayed: false, state: .downloading
        ))
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
