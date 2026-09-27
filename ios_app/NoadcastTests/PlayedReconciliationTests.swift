import Testing
import Foundation
import SwiftData
@testable import Noadcast

struct PlayedReconciliationTests {
    @Test @MainActor func snapshotUsesPlayedIntentAndCurrentServerWork() async throws {
        let container = try makeTestContainer()
        let engine = SyncEngine(modelContainer: container)
        _ = try await engine.apply(
            page: SyncFixtures.page(
                podcasts: [SyncFixtures.podcast(1)],
                episodes: [
                    SyncFixtures.episode(10, podcast: 1, audioState: .present),
                    SyncFixtures.episode(11, podcast: 1, state: .transcribing, audioState: .absent),
                    SyncFixtures.episode(12, podcast: 1, audioState: .absent),
                    SyncFixtures.episode(13, podcast: 1, audioState: .present),
                    SyncFixtures.episode(14, podcast: 1, audioState: .evicted),
                    SyncFixtures.episode(15, podcast: 1, audioState: .present),
                ], nextSince: 20
            ), options: SyncFixtures.options, save: true
        )
        let context = container.mainContext
        for id in [10, 11, 12, 14] {
            let episode = try #require(try storedEpisode(id, in: context))
            episode.isPlayed = true
        }
        let queued = try #require(try storedEpisode(12, in: context))
        context.insert(QueueItem(position: 0, episode: queued))
        let oldFile = try #require(try storedEpisode(14, in: context))
        oldFile.localFilename = "legacy-played.mp3"
        // Episode 13 is server-present but never downloaded and unplayed;
        // episode 15 has a local file but is still unplayed. Neither releases.
        let unplayed = try #require(try storedEpisode(15, in: context))
        unplayed.localFilename = "keep-unplayed.mp3"
        try context.save()

        let snapshot = try await engine.playedReconciliationSnapshot()

        #expect(snapshot.releaseEpisodeIDs == [10, 11])
        #expect(snapshot.localCleanupEpisodeIDs == [12, 14])
        let dirty = await engine.hasUnsavedChanges()
        #expect(!dirty)
    }
}

/// Shares NetworkStubTests' serialized suite because SyncHarness also uses
/// process-wide sync flags while the HTTP origin and release outbox are private.
extension NetworkStubTests {
    private func waitForRequests(_ count: Int, host: String, path: String) async throws {
        for _ in 0..<200 {
            let matching = StubServer.shared.requests(for: host).filter { $0.url?.path == path }
            if matching.count >= count { return }
            try await Task.sleep(for: .milliseconds(10))
        }
        throw URLError(.timedOut)
    }

    @Test @MainActor func emptyDeltaReconstructsAndFlushesMissedRelease() async throws {
        let host = StubServer.uniqueHost()
        defer { StubServer.shared.unregister(host: host) }
        let first = StubJSON.syncPage(
            instanceId: "played-instance",
            podcasts: [StubJSON.podcast(id: 1, feedUrl: "https://example.invalid/feed", title: "Played")],
            episodes: [StubJSON.episode(id: 10, podcastId: 1, guid: "played-10", title: "Played episode")],
            nextSince: 10, hasMore: false
        )
        let empty = StubJSON.syncPage(instanceId: "played-instance", nextSince: 10, hasMore: false)
        StubServer.shared.register(host: host) { request, _ in
            switch request.url?.path {
            case "/api/v1/sync":
                return .json(StubServer.queryValue("since", in: request) == "0" ? first : empty)
            case "/api/v1/episodes/10/audio":
                return .json("", status: 204)
            default: return .notFound
            }
        }
        let harness = try SyncHarness(host: host)
        defer { harness.cleanUp() }
        await harness.service.syncNow(.manual)
        let episode = try #require(try storedEpisode(10, in: harness.container.mainContext))
        episode.isPlayed = true
        try harness.container.mainContext.save()
        #expect(PendingReleaseStore.load(defaults: harness.releaseDefaults).ids.isEmpty)

        await harness.service.syncNow(.manual)

        #expect(harness.service.lastError == nil)
        let deletes = StubServer.shared.requests(for: host).filter {
            $0.httpMethod == "DELETE" && $0.url?.path == "/api/v1/episodes/10/audio"
        }
        #expect(deletes.count == 1)
        #expect(PendingReleaseStore.load(defaults: harness.releaseDefaults).ids.isEmpty)
    }

    @Test @MainActor func offlineReleaseRetriesAndRevivalStopsFutureReconstruction() async throws {
        let host = StubServer.uniqueHost()
        defer { StubServer.shared.unregister(host: host) }
        let first = StubJSON.syncPage(
            instanceId: "retry-instance",
            podcasts: [StubJSON.podcast(id: 2, feedUrl: "https://example.invalid/retry", title: "Retry")],
            episodes: [StubJSON.episode(id: 20, podcastId: 2, guid: "retry-20", title: "Retry episode")],
            nextSince: 20, hasMore: false
        )
        let empty = StubJSON.syncPage(instanceId: "retry-instance", nextSince: 20, hasMore: false)
        let failDelete = LockedBox(true)
        StubServer.shared.register(host: host) { request, _ in
            switch request.url?.path {
            case "/api/v1/sync":
                return .json(StubServer.queryValue("since", in: request) == "0" ? first : empty)
            case "/api/v1/episodes/20/audio":
                return failDelete.value ? .json(StubJSON.error(code: "unavailable", message: "Offline"), status: 503)
                    : .json("", status: 204)
            default: return .notFound
            }
        }
        let harness = try SyncHarness(host: host)
        defer { harness.cleanUp() }
        await harness.service.syncNow(.manual)
        let episode = try #require(try storedEpisode(20, in: harness.container.mainContext))
        episode.isPlayed = true
        try harness.container.mainContext.save()

        await harness.service.syncNow(.manual)
        #expect(PendingReleaseStore.load(defaults: harness.releaseDefaults).ids == [20])
        failDelete.withLock { $0 = false }
        await harness.service.syncNow(.manual)
        #expect(PendingReleaseStore.load(defaults: harness.releaseDefaults).ids.isEmpty)

        episode.isPlayed = false
        try harness.container.mainContext.save()
        await harness.service.syncNow(.manual)
        let deletes = StubServer.shared.requests(for: host).filter { $0.httpMethod == "DELETE" }
        #expect(deletes.count == 2)
    }

    @Test @MainActor func remarkDuringInflightDeleteKeepsNewReleaseIntent() async throws {
        let host = StubServer.uniqueHost()
        defer { StubServer.shared.unregister(host: host) }
        let page = StubJSON.syncPage(instanceId: "remark", nextSince: 1, hasMore: false)
        let firstDelete = StubDeliveryGate()
        let deleteCount = LockedBox(0)
        StubServer.shared.register(host: host) { request, _ in
            switch request.url?.path {
            case "/api/v1/sync": return .json(page)
            case "/api/v1/episodes/42/audio":
                let number = deleteCount.withLock { $0 += 1; return $0 }
                return .json("", status: 204, deliveryGate: number == 1 ? firstDelete : nil)
            default: return .notFound
            }
        }
        let harness = try SyncHarness(host: host)
        defer { harness.cleanUp() }
        await harness.service.syncNow(.manual)
        PendingReleaseStore.add(42, instanceId: "remark", defaults: harness.releaseDefaults)
        let oldGeneration = try #require(PendingReleaseStore.generation(for: 42, defaults: harness.releaseDefaults))
        let flush = Task { await harness.service.flushPendingReleases() }
        try await waitForRequests(1, host: host, path: "/api/v1/episodes/42/audio")
        PendingReleaseStore.add(42, instanceId: "remark", defaults: harness.releaseDefaults)
        #expect(PendingReleaseStore.generation(for: 42, defaults: harness.releaseDefaults) != oldGeneration)
        await firstDelete.open()
        await flush.value

        #expect(deleteCount.value == 2)
        #expect(PendingReleaseStore.load(defaults: harness.releaseDefaults).ids.isEmpty)
    }

    @Test @MainActor func episodeMutationsWaitForReleaseWithoutBlockingOtherEpisodes() async throws {
        let host = StubServer.uniqueHost()
        defer { StubServer.shared.unregister(host: host) }
        let firstDelete = StubDeliveryGate()
        let secondDelete = StubDeliveryGate()
        let deleteCount = LockedBox(0)
        StubServer.shared.register(host: host) { request, _ in
            switch request.url?.path {
            case "/api/v1/episodes/42/audio" where request.httpMethod == "DELETE":
                let number = deleteCount.withLock { $0 += 1; return $0 }
                return .json("", status: 204, deliveryGate: number == 1 ? firstDelete : secondDelete)
            case "/api/v1/episodes/42/process", "/api/v1/episodes/43/process",
                 "/api/v1/episodes/42/reanalyze" where request.httpMethod == "POST":
                return .json(#"{"jobId": 7}"#)
            default: return .notFound
            }
        }
        let client = try makeStubClient(host: host)
        let release = Task { try await client.releaseAudio(episodeID: 42, reason: .played) }
        try await waitForRequests(1, host: host, path: "/api/v1/episodes/42/audio")
        let sameEpisode = Task { try await client.process(episodeID: 42) }
        let otherEpisode = Task { try await client.process(episodeID: 43) }
        try await waitForRequests(1, host: host, path: "/api/v1/episodes/43/process")
        #expect(StubServer.shared.requests(for: host).allSatisfy {
            $0.url?.path != "/api/v1/episodes/42/process"
        })
        await firstDelete.open()
        try await release.value
        _ = try await sameEpisode.value
        _ = try await otherEpisode.value
        let paths = StubServer.shared.requests(for: host).compactMap { $0.url?.path }
        let releaseIndex = try #require(paths.firstIndex(of: "/api/v1/episodes/42/audio"))
        let processIndex = try #require(paths.firstIndex(of: "/api/v1/episodes/42/process"))
        #expect(releaseIndex < processIndex)

        let secondRelease = Task { try await client.releaseAudio(episodeID: 42, reason: .played) }
        try await waitForRequests(2, host: host, path: "/api/v1/episodes/42/audio")
        let canceled = Task { try await client.reanalyze(episodeID: 42) }
        canceled.cancel()
        await secondDelete.open()
        try await secondRelease.value
        do {
            _ = try await canceled.value
            Issue.record("Canceled waiting mutation unexpectedly completed")
        } catch let error as APIError {
            #expect(error == .cancelled)
        }
        #expect(StubServer.shared.requests(for: host).allSatisfy {
            $0.url?.path != "/api/v1/episodes/42/reanalyze"
        })
    }

    @Test @MainActor func instanceReplacementDropsOldReleaseIDs() async throws {
        let host = StubServer.uniqueHost()
        defer { StubServer.shared.unregister(host: host) }
        let pageA = StubJSON.syncPage(
            instanceId: "A",
            podcasts: [StubJSON.podcast(id: 1, feedUrl: "https://example.invalid/old", title: "Old")],
            episodes: [StubJSON.episode(id: 10, podcastId: 1, guid: "old-guid", title: "Old episode")],
            nextSince: 10, hasMore: false
        )
        StubServer.shared.register(host: host) { request, _ in
            request.url?.path == "/api/v1/sync" ? .json(pageA) : .notFound
        }
        let harness = try SyncHarness(host: host)
        defer { harness.cleanUp() }
        await harness.service.syncNow(.manual)
        let old = try #require(try storedEpisode(10, in: harness.container.mainContext))
        old.isPlayed = true
        try harness.container.mainContext.save()
        PendingReleaseStore.add(10, instanceId: "A", defaults: harness.releaseDefaults)

        let pageB = StubJSON.syncPage(instanceId: "B", nextSince: 20, hasMore: false)
        StubServer.shared.register(host: host) { request, _ in
            guard request.url?.path == "/api/v1/sync" else { return .notFound }
            return .json(pageB)
        }
        await harness.service.syncNow(.manual)

        #expect(harness.service.instanceId == "B")
        #expect(PendingReleaseStore.load(defaults: harness.releaseDefaults).ids.isEmpty)
        #expect(StubServer.shared.requests(for: host).filter { $0.httpMethod == "DELETE" }.isEmpty)
    }
}
