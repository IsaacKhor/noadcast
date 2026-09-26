//
//  SyncEngineTests.swift
//  NoadcastTests
//
//  `SyncEngine.apply(page:options:save:)` against an in-memory store:
//  idempotent upserts, write-if-changed, field ownership, marker
//  replacement, and deletions. Results are always read back through a fresh
//  `ModelContext`, never through the engine's own context.
//

import Testing
import Foundation
import SwiftData
@testable import Noadcast

struct SyncEngineTests {

    // MARK: - Upserts

    @Test @MainActor func applyingTheSamePageTwiceIsIdempotent() async throws {
        let container = try makeTestContainer()
        let engine = SyncEngine(modelContainer: container)
        let page = SyncFixtures.page(
            podcasts: [SyncFixtures.podcast(1)],
            episodes: [
                SyncFixtures.episode(10, podcast: 1, markerRevision: 1, markers: [SyncFixtures.marker(0, 30, .intro, summary: "Intro")]),
                SyncFixtures.episode(11, podcast: 1),
            ],
            settings: ServerSettingsDTO(adAnalysisEnabled: true, autoProcessEnabled: true, classifier: "fake"),
            nextSince: 100
        )

        let first = try await engine.apply(page: page, options: SyncFixtures.options, save: true)
        #expect(first.insertedPodcastIDs == [1])
        #expect(first.insertedEpisodeIDs == [10, 11])
        let countsAfterFirst = await engine.mirrorCounts()
        #expect(countsAfterFirst == MirrorCounts(podcasts: 1, episodes: 2, markers: 1, queueItems: 0))

        let second = try await engine.apply(page: page, options: SyncFixtures.options, save: true)
        #expect(second.insertedPodcastIDs.isEmpty)
        #expect(second.insertedEpisodeIDs.isEmpty)
        let countsAfterSecond = await engine.mirrorCounts()
        #expect(countsAfterSecond == countsAfterFirst)

        // Duplicates within a page are harmless too: the last occurrence wins.
        let repeated = SyncFixtures.page(
            podcasts: [SyncFixtures.podcast(1), SyncFixtures.podcast(1)],
            episodes: [
                SyncFixtures.episode(11, podcast: 1, title: "Draft title"),
                SyncFixtures.episode(11, podcast: 1, title: "Final title"),
            ],
            nextSince: 101
        )
        let third = try await engine.apply(page: repeated, options: SyncFixtures.options, save: true)
        #expect(third.insertedPodcastIDs.isEmpty)
        #expect(third.insertedEpisodeIDs.isEmpty)
        #expect(third.changedEpisodeIDs == [11])
        let countsAfterThird = await engine.mirrorCounts()
        #expect(countsAfterThird == countsAfterFirst)

        let context = ModelContext(container)
        let episodes = try context.fetch(FetchDescriptor<Episode>(sortBy: [SortDescriptor(\Episode.serverID)]))
        let episodeIDs = episodes.map(\.serverID)
        #expect(episodeIDs == [10, 11])
        #expect(episodes.last?.title == "Final title")
        #expect(try context.fetchCount(FetchDescriptor<Podcast>()) == 1)
        #expect(try context.fetchCount(FetchDescriptor<AppSettings>()) == 1)
        #expect(try context.fetchCount(FetchDescriptor<SyncCursor>()) == 1)
    }

    // MARK: - Write-if-changed

    @Test @MainActor func reapplyingAnUnchangedPageWritesNothing() async throws {
        let container = try makeTestContainer()
        let engine = SyncEngine(modelContainer: container)
        let page = SyncFixtures.page(
            podcasts: [SyncFixtures.podcast(1), SyncFixtures.podcast(2)],
            episodes: [
                SyncFixtures.episode(
                    10,
                    podcast: 1,
                    markerRevision: 2,
                    markers: [SyncFixtures.marker(0, 45, .intro, summary: "Theme"), SyncFixtures.marker(600, 660)]
                ),
                SyncFixtures.episode(11, podcast: 1, state: .classifying),
                SyncFixtures.episode(20, podcast: 2, audioState: .evicted),
            ],
            settings: ServerSettingsDTO(
                adAnalysisEnabled: false,
                autoProcessEnabled: true,
                classifier: "gemini",
                classifierModel: "gemini-3.5-flash"
            ),
            nextSince: 500
        )

        let first = try await engine.apply(page: page, options: SyncFixtures.options, save: true)
        #expect(first.didSave)
        let dirtyAfterFirstSave = await engine.hasUnsavedChanges()
        #expect(!dirtyAfterFirstSave)

        let unchanged = try await engine.apply(page: page, options: SyncFixtures.options, save: false)
        let dirtyAfterNoOp = await engine.hasUnsavedChanges()
        #expect(!dirtyAfterNoOp, "Re-applying an identical page must not assign to any model")
        #expect(!unchanged.hasMirrorChanges)
        #expect(unchanged.changedPodcastIDs.isEmpty)
        #expect(unchanged.changedEpisodeIDs.isEmpty)
        #expect(unchanged.markersChangedEpisodeIDs.isEmpty)
        #expect(!unchanged.settingsChanged)
        #expect(!unchanged.didSave)

        var episodes = page.episodes
        episodes[1] = SyncFixtures.episode(11, podcast: 1, title: "Episode 11 (remastered)", state: .classifying)
        let titleOnly = SyncFixtures.page(
            podcasts: page.podcasts,
            episodes: episodes,
            settings: page.settings,
            nextSince: 500
        )
        let retitled = try await engine.apply(page: titleOnly, options: SyncFixtures.options, save: false)
        let dirtyAfterTitleChange = await engine.hasUnsavedChanges()
        #expect(dirtyAfterTitleChange)
        #expect(retitled.changedEpisodeIDs == [11])
        #expect(retitled.changedPodcastIDs.isEmpty)
        #expect(retitled.insertedEpisodeIDs.isEmpty)
        #expect(retitled.markersChangedEpisodeIDs.isEmpty)
        #expect(!retitled.settingsChanged)
        #expect(retitled.hasMirrorChanges)
    }

    // MARK: - Ownership

    @Test @MainActor func syncNeverClobbersDeviceLocalFields() async throws {
        let container = try makeTestContainer()
        let engine = SyncEngine(modelContainer: container)
        _ = try await engine.apply(
            page: SyncFixtures.page(
                podcasts: [SyncFixtures.podcast(1, title: "Old Show")],
                episodes: [
                    SyncFixtures.episode(
                        10,
                        podcast: 1,
                        title: "Old title",
                        state: .downloading,
                        audioState: .absent,
                        markerRevision: 1,
                        markers: [SyncFixtures.marker(10, 20)]
                    ),
                ],
                nextSince: 1
            ),
            options: SyncFixtures.options,
            save: true
        )

        // The app writes device-local state on the main context.
        let filename = "noadcast-test-\(UUID().uuidString).mp3"
        let main = container.mainContext
        let fetchedEpisode = try storedEpisode(10, in: main)
        let localEpisode = try #require(fetchedEpisode)
        localEpisode.playbackPosition = 42
        localEpisode.isPlayed = true
        localEpisode.datePlayed = SyncFixtures.now
        localEpisode.localFilename = filename
        localEpisode.fileSizeBytes = 28_800_000
        let fetchedPodcast = try storedPodcast(1, in: main)
        let localPodcast = try #require(fetchedPodcast)
        localPodcast.autoDownloadEnabled = false
        localPodcast.customPlaybackSpeed = 2.0
        try main.save()

        // Every server field changes.
        let report = try await engine.apply(
            page: SyncFixtures.page(
                podcasts: [SyncFixtures.podcast(1, title: "New Show")],
                episodes: [
                    SyncFixtures.episode(
                        10,
                        podcast: 1,
                        title: "New title",
                        state: .ready,
                        audioState: .present,
                        markerRevision: 2,
                        markers: [SyncFixtures.marker(30, 40), SyncFixtures.marker(1_700, 1_800, .outro, summary: "Credits")]
                    ),
                ],
                nextSince: 2
            ),
            options: SyncFixtures.options,
            save: true
        )
        #expect(report.changedPodcastIDs == [1])
        #expect(report.changedEpisodeIDs == [10])
        #expect(report.markersChangedEpisodeIDs == [10])
        #expect(report.audioBecamePresentEpisodeIDs == [10])

        let context = ModelContext(container)
        let fetchedResult = try storedEpisode(10, in: context)
        let episode = try #require(fetchedResult)
        // Device-local: untouched.
        #expect(episode.playbackPosition == 42)
        #expect(episode.isPlayed)
        #expect(episode.datePlayed == SyncFixtures.now)
        #expect(episode.localFilename == filename)
        #expect(episode.fileSizeBytes == 28_800_000)
        // Server mirror: updated.
        #expect(episode.title == "New title")
        #expect(episode.serverState == .ready)
        #expect(episode.audioState == .present)
        #expect(episode.markerRevision == 2)
        #expect(episode.activeAdMarkerCount == 2)
        #expect(markerSignatures(of: episode) == [
            MarkerSignature(startSeconds: 30, endSeconds: 40, kindRaw: "ad", summary: "Sponsor", manual: false),
            MarkerSignature(startSeconds: 1_700, endSeconds: 1_800, kindRaw: "outro", summary: "Credits", manual: false),
        ])
        // Denormalized podcast snapshot follows the server title.
        #expect(episode.podcastTitle == "New Show")

        let fetchedPodcastResult = try storedPodcast(1, in: context)
        let podcast = try #require(fetchedPodcastResult)
        #expect(!podcast.autoDownloadEnabled)
        #expect(podcast.customPlaybackSpeed == 2.0)
        #expect(podcast.title == "New Show")
    }

    // MARK: - Identity

    @Test @MainActor func sameGUIDUnderTwoPodcastsIsTwoEpisodes() async throws {
        let container = try makeTestContainer()
        let engine = SyncEngine(modelContainer: container)
        let page = SyncFixtures.page(
            podcasts: [
                SyncFixtures.podcast(1, feedURL: "https://a.example.com/feed.xml"),
                SyncFixtures.podcast(2, feedURL: "https://b.example.com/feed.xml"),
            ],
            episodes: [
                SyncFixtures.episode(10, podcast: 1, guid: "episode-1"),
                SyncFixtures.episode(20, podcast: 2, guid: "episode-1"),
            ],
            nextSince: 7
        )

        let report = try await engine.apply(page: page, options: SyncFixtures.options, save: true)
        #expect(report.insertedEpisodeIDs == [10, 20])
        #expect(report.orphanedEpisodeIDs.isEmpty)
        _ = try await engine.apply(page: page, options: SyncFixtures.options, save: true)

        let context = ModelContext(container)
        let guid = "episode-1"
        let shared = try context.fetch(FetchDescriptor<Episode>(
            predicate: #Predicate<Episode> { $0.guid == guid },
            sortBy: [SortDescriptor(\Episode.serverID)]
        ))
        let serverIDs = shared.map(\.serverID)
        let podcastServerIDs = shared.map(\.podcastServerID)
        let linkedPodcastIDs = shared.map { $0.podcast?.serverID }
        #expect(serverIDs == [10, 20])
        #expect(podcastServerIDs == [1, 2])
        #expect(linkedPodcastIDs == [1, 2])
    }

    // MARK: - Markers

    @Test @MainActor func markersAreReplacedWholesaleOnlyWhenTheyChange() async throws {
        let container = try makeTestContainer()
        let engine = SyncEngine(modelContainer: container)
        let podcast = SyncFixtures.podcast(6)
        func page(revision: Int, _ markers: [AdMarkerDTO], nextSince: Int) -> SyncPageDTO {
            SyncFixtures.page(
                podcasts: [podcast],
                episodes: [SyncFixtures.episode(60, podcast: 6, markerRevision: revision, markers: markers)],
                nextSince: nextSince
            )
        }
        let coldOpen = SyncFixtures.marker(0, 5, .intro, summary: "Cold open")
        let sponsorA = SyncFixtures.marker(100, 160, summary: "Sponsor A", manual: true)

        // 1. Insert.
        _ = try await engine.apply(page: page(revision: 1, [coldOpen, sponsorA], nextSince: 1), options: SyncFixtures.options, save: true)
        let insertedRead = try storedMarkerState(ofEpisode: 60, in: container)
        let inserted = try #require(insertedRead)
        #expect(inserted.activeAdMarkerCount == 2)
        #expect(inserted.markerRevision == 1)
        #expect(inserted.signatures == [
            MarkerSignature(startSeconds: 0, endSeconds: 5, kindRaw: "intro", summary: "Cold open", manual: false),
            MarkerSignature(startSeconds: 100, endSeconds: 160, kindRaw: "ad", summary: "Sponsor A", manual: true),
        ])
        #expect(inserted.rowIDs.count == 2)

        // 2. Identical set: rows untouched.
        let identical = try await engine.apply(page: page(revision: 1, [coldOpen, sponsorA], nextSince: 2), options: SyncFixtures.options, save: true)
        #expect(identical.markersChangedEpisodeIDs.isEmpty)
        #expect(identical.changedEpisodeIDs.isEmpty)
        let afterIdenticalRead = try storedMarkerState(ofEpisode: 60, in: container)
        let afterIdentical = try #require(afterIdenticalRead)
        #expect(afterIdentical.rowIDs == inserted.rowIDs)

        // 3. Same markerRevision and count: the fast path leaves the rows
        //    alone (the server bumps markerRevision on every marker change).
        let sponsorB = SyncFixtures.marker(100, 160, summary: "Sponsor B", manual: true)
        let sameRevision = try await engine.apply(page: page(revision: 1, [coldOpen, sponsorB], nextSince: 3), options: SyncFixtures.options, save: true)
        #expect(sameRevision.markersChangedEpisodeIDs.isEmpty)
        let afterSameRevisionRead = try storedMarkerState(ofEpisode: 60, in: container)
        let afterSameRevision = try #require(afterSameRevisionRead)
        #expect(afterSameRevision.rowIDs == inserted.rowIDs)
        #expect(afterSameRevision.signatures == inserted.signatures)

        // 4. Count differs (an invalid marker is dropped by sanitization):
        //    the whole set is replaced.
        let sponsorC = SyncFixtures.marker(200, 230, summary: "Sponsor C")
        let inverted = SyncFixtures.marker(50, 40, summary: "Inverted")
        let shrunk = try await engine.apply(page: page(revision: 2, [sponsorC, inverted], nextSince: 4), options: SyncFixtures.options, save: true)
        #expect(shrunk.markersChangedEpisodeIDs == [60])
        #expect(shrunk.changedEpisodeIDs == [60])
        let afterShrinkRead = try storedMarkerState(ofEpisode: 60, in: container)
        let afterShrink = try #require(afterShrinkRead)
        #expect(afterShrink.activeAdMarkerCount == 1)
        #expect(afterShrink.markerRevision == 2)
        #expect(afterShrink.signatures == [
            MarkerSignature(startSeconds: 200, endSeconds: 230, kindRaw: "ad", summary: "Sponsor C", manual: false),
        ])
        #expect(afterShrink.rowIDs.isDisjoint(with: inserted.rowIDs))
        let countsAfterShrink = await engine.mirrorCounts()
        #expect(countsAfterShrink.markers == 1)

        // 5. Same count, different contents, new revision: replaced.
        let moved = SyncFixtures.marker(205, 235, summary: "Sponsor C")
        let shifted = try await engine.apply(page: page(revision: 3, [moved], nextSince: 5), options: SyncFixtures.options, save: true)
        #expect(shifted.markersChangedEpisodeIDs == [60])
        let afterShiftRead = try storedMarkerState(ofEpisode: 60, in: container)
        let afterShift = try #require(afterShiftRead)
        #expect(afterShift.activeAdMarkerCount == 1)
        #expect(afterShift.markerRevision == 3)
        #expect(afterShift.signatures == [
            MarkerSignature(startSeconds: 205, endSeconds: 235, kindRaw: "ad", summary: "Sponsor C", manual: false),
        ])

        // 6. Cleared.
        let cleared = try await engine.apply(page: page(revision: 4, [], nextSince: 6), options: SyncFixtures.options, save: true)
        #expect(cleared.markersChangedEpisodeIDs == [60])
        let afterClearRead = try storedMarkerState(ofEpisode: 60, in: container)
        let afterClear = try #require(afterClearRead)
        #expect(afterClear.signatures.isEmpty)
        #expect(afterClear.activeAdMarkerCount == 0)
        #expect(afterClear.markerRevision == 4)
        let countsAfterClear = await engine.mirrorCounts()
        #expect(countsAfterClear.markers == 0)
    }

    // MARK: - Deletions

    @Test @MainActor func deletionsRemoveRowsAndQueueItemsAndCascadeFromPodcasts() async throws {
        let container = try makeTestContainer()
        let engine = SyncEngine(modelContainer: container)
        _ = try await engine.apply(
            page: SyncFixtures.page(
                podcasts: [SyncFixtures.podcast(1), SyncFixtures.podcast(2)],
                episodes: [
                    SyncFixtures.episode(10, podcast: 1, markerRevision: 1, markers: [SyncFixtures.marker(10, 20)]),
                    SyncFixtures.episode(11, podcast: 1),
                    SyncFixtures.episode(20, podcast: 2, markerRevision: 1, markers: [SyncFixtures.marker(30, 40)]),
                    SyncFixtures.episode(21, podcast: 2),
                ],
                nextSince: 10
            ),
            options: SyncFixtures.options,
            save: true
        )

        // Queue three of them on the device.
        let main = container.mainContext
        let mirrored = try main.fetch(FetchDescriptor<Episode>())
        let byID = Dictionary(uniqueKeysWithValues: mirrored.map { ($0.serverID, $0) })
        main.insert(QueueItem(position: 0, episode: byID[10]))
        main.insert(QueueItem(position: 1, episode: byID[11]))
        main.insert(QueueItem(position: 2, episode: byID[20]))
        try main.save()

        let report = try await engine.apply(
            page: SyncFixtures.page(
                deletions: [
                    DeletionDTO(entity: "episode", id: 10),
                    DeletionDTO(entity: "podcast", id: 2),
                    DeletionDTO(entity: "episode", id: 999),
                    DeletionDTO(entity: "transcript", id: 11),
                ],
                nextSince: 11
            ),
            options: SyncFixtures.options,
            save: true
        )
        #expect(Set(report.deletedEpisodeIDs) == [10, 20, 21])
        #expect(report.deletedPodcastIDs == [2])

        let context = ModelContext(container)
        let podcastIDs = try context.fetch(FetchDescriptor<Podcast>()).map(\.serverID)
        let episodeIDs = try context.fetch(FetchDescriptor<Episode>()).map(\.serverID)
        let queueItems = try context.fetch(FetchDescriptor<QueueItem>())
        #expect(podcastIDs == [1])
        #expect(episodeIDs == [11])
        #expect(queueItems.count == 1)
        #expect(queueItems.first?.episode?.serverID == 11)
        let counts = await engine.mirrorCounts()
        #expect(counts == MirrorCounts(podcasts: 1, episodes: 1, markers: 0, queueItems: 1))
    }

}
