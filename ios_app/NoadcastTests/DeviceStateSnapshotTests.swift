//
//  DeviceStateSnapshotTests.swift
//  NoadcastTests
//
//  The feedURL + guid snapshot that survives a mirror wipe, and the feed-URL
//  identity it is matched by.
//

import Testing
import Foundation
import SwiftData
@testable import Noadcast

struct DeviceStateSnapshotTests {

    @Test func snapshotRoundTripsThroughJSON() throws {
        let feed = "https://feeds.example.com/show.xml"
        let snapshot = DeviceStateSnapshot(
            source: .instanceChange,
            createdAt: Date(timeIntervalSinceReferenceDate: 811_792_991.25),
            podcasts: [
                DeviceStateSnapshot.PodcastState(
                    feedURL: feed,
                    title: "Show",
                    autoDownloadEnabled: false,
                    customPlaybackSpeed: 1.5,
                    adAnalysisEnabled: nil
                ),
                DeviceStateSnapshot.PodcastState(
                    feedURL: "https://other.example.com/rss",
                    title: nil,
                    autoDownloadEnabled: true,
                    customPlaybackSpeed: nil,
                    adAnalysisEnabled: false
                ),
            ],
            episodes: [
                DeviceStateSnapshot.EpisodeState(
                    feedURL: feed,
                    guid: "ep-1",
                    title: "One",
                    localFilename: "srv-10-abcd1234.mp3",
                    fileSizeBytes: 63_346_363,
                    audioMimeType: "audio/mpeg",
                    playbackPosition: 1_234.5,
                    isPlayed: false,
                    datePlayed: nil
                ),
                DeviceStateSnapshot.EpisodeState(
                    feedURL: feed,
                    guid: "ep-2",
                    title: nil,
                    localFilename: nil,
                    fileSizeBytes: nil,
                    audioMimeType: nil,
                    playbackPosition: 0,
                    isPlayed: true,
                    datePlayed: Date(timeIntervalSinceReferenceDate: 811_700_000)
                ),
            ],
            queue: [DeviceStateSnapshot.EpisodeKey(feedURL: feed, guid: "ep-1")],
            lastPlayed: DeviceStateSnapshot.EpisodeKey(feedURL: feed, guid: "ep-1"),
            stats: DeviceStateSnapshot.Stats(lifetimePlayedSeconds: 36_000, lifetimeAdSkipSeconds: 1_800),
            usageDays: [
                DeviceStateSnapshot.UsageDay(
                    dayStart: Date(timeIntervalSinceReferenceDate: 811_728_000),
                    playbackSeconds: 3_600,
                    adSkippedSeconds: 240
                ),
            ],
            preferences: DeviceStateSnapshot.Preferences(
                defaultPlaybackSpeed: 1.25,
                autoDownloadPolicy: AutoDownloadPolicy.wifiOnly.rawValue,
                autoDeleteAfterPlayed: true,
                podcastSortMode: PodcastSortMode.alphabetical.rawValue,
                skipAds: true,
                skipIntrosAndOutros: false,
                chainSkipGapSeconds: 5,
                adAnalysisEnabled: true
            )
        )

        let data = try DeviceStateSnapshot.encode(snapshot)
        let decoded = try DeviceStateSnapshot.decode(data)

        #expect(decoded == snapshot)
        #expect(decoded.version == 1)
        #expect(decoded.source == .instanceChange)
        #expect(!decoded.isEmpty)
        #expect(decoded.episodes.first?.key == DeviceStateSnapshot.EpisodeKey(feedURL: feed, guid: "ep-1"))
        #expect(DeviceStateSnapshot(source: .localReset).isEmpty)
    }

    @Test func feedURLKeyTreatsCosmeticRewritesAsEqual() {
        let canonical = FeedURLKey.normalize("https://example.com/feed.xml")
        #expect(canonical == "example.com/feed.xml")

        let variants = [
            "http://example.com/feed.xml",
            "https://EXAMPLE.com/feed.xml",
            "https://www.example.com/feed.xml",
            "https://example.com:443/feed.xml",
            "http://example.com:80/feed.xml",
            "https://example.com/feed.xml/",
            "  http://www.Example.COM:80/feed.xml//  ",
        ]
        for variant in variants {
            #expect(FeedURLKey.normalize(variant) == canonical, "\(variant)")
        }

        // Real differences stay different: a non-default port, another path,
        // or another query (it can select a different feed).
        #expect(FeedURLKey.normalize("https://example.com:8443/feed.xml") != canonical)
        #expect(FeedURLKey.normalize("https://example.com/other.xml") != canonical)
        #expect(FeedURLKey.normalize("https://example.com/feed.xml?id=1") != FeedURLKey.normalize("https://example.com/feed.xml?id=2"))
        #expect(FeedURLKey.normalize("https://example.com/feed/?id=1") == FeedURLKey.normalize("http://www.example.com/feed?id=1"))
    }

    @Test func episodeMatchKeyUsesTheNormalisedFeed() {
        let original = DeviceStateSnapshot.EpisodeKey(feedURL: "http://www.Example.com/feed/", guid: "tal-646")
        let rewritten = DeviceStateSnapshot.EpisodeKey(feedURL: "https://example.com/feed", guid: "tal-646")
        let otherGUID = DeviceStateSnapshot.EpisodeKey(feedURL: "https://example.com/feed", guid: "TAL-646")

        #expect(original.matchKey == rewritten.matchKey)
        #expect(original.matchKey != otherGUID.matchKey)
        // The stored key keeps the original spelling; only matching normalises.
        #expect(original != rewritten)
    }

    @Test func oldStoreStartsAtFreshPathWithoutOpeningOrChangingIt() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        let suite = "LocalStoreGenerationTests.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suite))
        defer {
            defaults.removePersistentDomain(forName: suite)
            try? FileManager.default.removeItem(at: directory)
        }
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let oldStore = directory.appendingPathComponent("default.store")
        let oldBytes = Data("old, incompatible store".utf8)
        try oldBytes.write(to: oldStore)

        LocalStoreGeneration.prepare(
            defaultStoreURL: oldStore,
            defaults: defaults,
            jobsDirectory: directory.appendingPathComponent("pending-restores")
        )

        #expect(defaults.integer(forKey: LocalStoreGeneration.generationKey) == 2)
        #expect(LocalStoreGeneration.storeURL(defaultStoreURL: oldStore, defaults: defaults)
            == directory.appendingPathComponent("noadcast-server.store"))
        #expect(try Data(contentsOf: oldStore) == oldBytes)

        // A later launch must keep using the new path, even while the old
        // database still exists.
        LocalStoreGeneration.prepare(
            defaultStoreURL: oldStore,
            defaults: defaults,
            jobsDirectory: directory.appendingPathComponent("pending-restores")
        )
        #expect(LocalStoreGeneration.storeURL(defaultStoreURL: oldStore, defaults: defaults)
            == directory.appendingPathComponent("noadcast-server.store"))
    }

    @Test func currentStoreAndNonLegacyRestoreJobSurvivePreparation() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        let suite = "LocalStoreGenerationTests.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suite))
        defer {
            defaults.removePersistentDomain(forName: suite)
            try? FileManager.default.removeItem(at: directory)
        }
        let jobsDirectory = directory.appendingPathComponent("pending-restores")
        try FileManager.default.createDirectory(at: jobsDirectory, withIntermediateDirectories: true)
        let currentStore = directory.appendingPathComponent("default.store")
        let currentBytes = Data("current store".utf8)
        try currentBytes.write(to: currentStore)
        defaults.set(LocalStoreGeneration.currentGeneration, forKey: LocalStoreGeneration.generationKey)
        defaults.set("exported", forKey: LocalStoreGeneration.outcomeKey)
        defaults.set("Migration in progress", forKey: DeviceStateRestoreService.statusDefaultsKey)

        let oldJob = RestoreJob(snapshot: DeviceStateSnapshot(source: .legacyStore), subscribeMissingFeeds: true, scanLegacyFilenames: false)
        let currentJob = RestoreJob(snapshot: DeviceStateSnapshot(source: .instanceChange), subscribeMissingFeeds: false, scanLegacyFilenames: false)
        let oldJobURL = jobsDirectory.appendingPathComponent("\(oldJob.id).json")
        let currentJobURL = jobsDirectory.appendingPathComponent("\(currentJob.id).json")
        try JSONEncoder().encode(oldJob).write(to: oldJobURL)
        try JSONEncoder().encode(currentJob).write(to: currentJobURL)

        LocalStoreGeneration.prepare(defaultStoreURL: currentStore, defaults: defaults, jobsDirectory: jobsDirectory)

        #expect(LocalStoreGeneration.storeURL(defaultStoreURL: currentStore, defaults: defaults) == currentStore)
        #expect(try Data(contentsOf: currentStore) == currentBytes)
        #expect(!FileManager.default.fileExists(atPath: oldJobURL.path))
        #expect(FileManager.default.fileExists(atPath: currentJobURL.path))
        #expect(defaults.string(forKey: DeviceStateRestoreService.statusDefaultsKey) == nil)
        #expect(defaults.bool(forKey: DeviceStateRestoreService.legacyImportedDefaultsKey))
    }

    @Test func staleMigrationMessageClearsEvenWithoutOutcomeMetadata() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        let suite = "LocalStoreGenerationTests.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suite))
        defer {
            defaults.removePersistentDomain(forName: suite)
            try? FileManager.default.removeItem(at: directory)
        }
        let jobsDirectory = directory.appendingPathComponent("pending-restores")
        try FileManager.default.createDirectory(at: jobsDirectory, withIntermediateDirectories: true)
        try Data("{malformed legacy job".utf8).write(to: jobsDirectory.appendingPathComponent("broken.json"))
        defaults.set("Migration in progress: 0 of 5 episodes restored", forKey: DeviceStateRestoreService.statusDefaultsKey)
        LocalStoreGeneration.prepare(
            defaultStoreURL: directory.appendingPathComponent("default.store"),
            defaults: defaults,
            jobsDirectory: jobsDirectory
        )
        #expect(defaults.string(forKey: DeviceStateRestoreService.statusDefaultsKey) == nil)
    }

    @Test @MainActor func restoreJobProgressWaitsForSwiftDataSave() async throws {
        enum SaveFailure: Error { case expected }
        let container = try makeTestContainer()
        let context = container.mainContext
        let feed = try #require(URL(string: "https://feeds.example.com/show.xml"))
        let podcast = Podcast(serverID: 1, feedURL: feed, title: "Show")
        context.insert(podcast)
        try context.save()

        let jobsDirectory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        defer { try? FileManager.default.removeItem(at: jobsDirectory) }
        let client = try makeStubClient(host: "restore-save-test.invalid")
        var failNextSave = true
        let restore = DeviceStateRestoreService(
            api: client,
            jobsDirectory: jobsDirectory,
            performsSideEffects: false,
            isConfigured: { true },
            saveContext: { context in
                if failNextSave {
                    failNextSave = false
                    throw SaveFailure.expected
                }
                try context.save()
            }
        )
        restore.configure(container: container)
        let snapshot = DeviceStateSnapshot(
            source: .instanceChange,
            podcasts: [DeviceStateSnapshot.PodcastState(
                feedURL: feed.absoluteString,
                title: "Show",
                autoDownloadEnabled: false,
                customPlaybackSpeed: nil,
                adAnalysisEnabled: nil
            )]
        )
        restore.enqueue(snapshot, subscribeMissingFeeds: false)

        await restore.runPendingRestores()
        let pendingAfterFailure = restore.hasPendingJobs
        let failedReadContext = ModelContext(container)
        let afterFailedSave = try storedPodcast(1, in: failedReadContext)
        let failedPodcast = try #require(afterFailedSave)
        let downloadEnabledAfterFailure = failedPodcast.autoDownloadEnabled
        #expect(pendingAfterFailure)
        #expect(downloadEnabledAfterFailure)
        #expect(try FileManager.default.contentsOfDirectory(atPath: jobsDirectory.path).count == 1)

        await restore.runPendingRestores()
        let pendingAfterSuccess = restore.hasPendingJobs
        let successfulReadContext = ModelContext(container)
        let afterSuccessfulSave = try storedPodcast(1, in: successfulReadContext)
        let successfulPodcast = try #require(afterSuccessfulSave)
        let downloadEnabledAfterSuccess = successfulPodcast.autoDownloadEnabled
        #expect(!pendingAfterSuccess)
        #expect(!downloadEnabledAfterSuccess)
        #expect(try FileManager.default.contentsOfDirectory(atPath: jobsDirectory.path).isEmpty)
    }

    @Test func localHashVerificationRejectsEqualSizedDifferentAudio() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let localURL = directory.appendingPathComponent("local.mp3")
        let serverURL = directory.appendingPathComponent("server.mp3")
        try Data("old ad audio".utf8).write(to: localURL)
        try Data("new ad audio".utf8).write(to: serverURL)
        let localHash = try AudioStorage.sha256(of: localURL)
        let serverHash = try AudioStorage.sha256(of: serverURL)

        let localSize = try Data(contentsOf: localURL).count
        let serverSize = try Data(contentsOf: serverURL).count
        let mismatched = await DeviceStateRestoreService.verifiedLocalHash(at: localURL, expectedServerHash: serverHash)
        let matched = await DeviceStateRestoreService.verifiedLocalHash(at: localURL, expectedServerHash: localHash)
        let withoutServerHash = await DeviceStateRestoreService.verifiedLocalHash(at: localURL, expectedServerHash: nil)
        #expect(localSize == serverSize)
        #expect(mismatched == nil)
        #expect(matched == localHash)
        #expect(withoutServerHash == localHash)
    }

}
