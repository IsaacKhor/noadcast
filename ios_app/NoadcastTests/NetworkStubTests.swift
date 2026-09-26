//
//  NetworkStubTests.swift
//  NoadcastTests
//
//  Everything that goes over `StubURLProtocol`: the API client's retry and
//  401 handling, and `SyncService` driving real `/sync` paging and an
//  `instanceId` change end to end. Serialized, and every test uses its own
//  host, so no stub can ever answer another test's request.
//

import Testing
import Foundation
import SwiftData
@testable import Noadcast

@Suite(.serialized)
struct NetworkStubTests {

    private static let fastRetry = RetryPolicy(maxAttempts: 3, baseDelay: 0, maxDelay: 0, maxRetryAfter: 0)
    private static let settingsJSON = #"{"adAnalysisEnabled": false, "autoProcessEnabled": true, "classifier": "fake", "classifierModel": null, "availableClassifiers": {"fake": true}}"#

    // MARK: - API client

    @Test func clientRetriesAServerErrorThenSucceeds() async throws {
        let host = StubServer.uniqueHost()
        defer { StubServer.shared.unregister(host: host) }
        let settingsJSON = Self.settingsJSON
        StubServer.shared.register(host: host) { _, index in
            if index == 0 {
                return .json(StubJSON.error(code: "unavailable", message: "Warming up"), status: 503)
            }
            return .json(settingsJSON)
        }
        let client = try makeStubClient(host: host, retryPolicy: Self.fastRetry)

        let settings = try await client.settings()

        #expect(!settings.adAnalysisEnabled)
        #expect(settings.classifier == "fake")
        let requests = StubServer.shared.requests(for: host)
        let paths = requests.map { $0.url?.path }
        #expect(paths == ["/api/v1/settings", "/api/v1/settings"])
        #expect(requests.first?.value(forHTTPHeaderField: "Authorization") == "Bearer test-token")
    }

    @Test func clientReportsUnauthorizedOnceAndNeverRetriesIt() async throws {
        let host = StubServer.uniqueHost()
        defer { StubServer.shared.unregister(host: host) }
        StubServer.shared.register(host: host) { _, _ in
            .json(
                StubJSON.error(code: "unauthorized", message: "Bad token"),
                status: 401,
                headers: ["WWW-Authenticate": "Bearer"]
            )
        }
        let authEvents = LockedBox<[Bool]>([])
        let client = try makeStubClient(
            host: host,
            retryPolicy: Self.fastRetry,
            authStateHandler: { failed in
                authEvents.withLock { $0.append(failed) }
            }
        )

        do {
            _ = try await client.settings()
            Issue.record("Expected APIError.unauthorized")
        } catch let error as APIError {
            #expect(error == .unauthorized)
        }
        #expect(StubServer.shared.requests(for: host).count == 1)
        #expect(authEvents.value == [true])

        // The handler fires on transitions only: once on recovery, then quiet.
        let settingsJSON = Self.settingsJSON
        StubServer.shared.register(host: host) { _, _ in .json(settingsJSON) }
        _ = try await client.settings()
        _ = try await client.settings()
        #expect(authEvents.value == [true, false])
    }

    // MARK: - SyncService

    @Test @MainActor func syncFollowsHasMoreAndPersistsTheFinalCursor() async throws {
        let host = StubServer.uniqueHost()
        defer { StubServer.shared.unregister(host: host) }
        let podcast = StubJSON.podcast(id: 1, feedUrl: "https://feeds.example.com/paging.xml", title: "Paging")
        let firstPage = StubJSON.syncPage(
            instanceId: "paging-instance",
            podcasts: [podcast],
            episodes: [StubJSON.episode(id: 10, podcastId: 1, guid: "paging-10", title: "First")],
            nextSince: 10,
            hasMore: true
        )
        // Referential closure: the podcast comes again with its episode.
        let secondPage = StubJSON.syncPage(
            instanceId: "paging-instance",
            podcasts: [podcast],
            episodes: [StubJSON.episode(id: 11, podcastId: 1, guid: "paging-11", title: "Second")],
            nextSince: 20,
            hasMore: false
        )
        StubServer.shared.register(host: host) { request, _ in
            guard request.url?.path == "/api/v1/sync" else { return .notFound }
            switch StubServer.queryValue("since", in: request) ?? "" {
            case "0": return .json(firstPage)
            case "10": return .json(secondPage)
            default: return .json(StubJSON.error(code: "invalidRequest", message: "Unexpected cursor"), status: 400)
            }
        }
        let harness = try SyncHarness(host: host)
        defer { harness.cleanUp() }

        await harness.service.syncNow(.manual)

        #expect(harness.service.lastError == nil)
        let sinceValues = StubServer.shared.requests(for: host).map { StubServer.queryValue("since", in: $0) }
        #expect(sinceValues == ["0", "10"])

        let context = ModelContext(harness.container)
        let episodeIDs = try context.fetch(FetchDescriptor<Episode>(sortBy: [SortDescriptor(\Episode.serverID)])).map(\.serverID)
        #expect(episodeIDs == [10, 11])
        let podcastCount = try context.fetchCount(FetchDescriptor<Podcast>())
        #expect(podcastCount == 1)

        let engine = try #require(harness.service.engine)
        let cursor = await engine.cursorState()
        #expect(cursor.nextSince == 20)
        #expect(cursor.instanceId == "paging-instance")
        #expect(cursor.lastSyncAt != nil)
    }

    @Test @MainActor func instanceChangeWipesTheMirrorAndReattachesDeviceState() async throws {
        let host = StubServer.uniqueHost()
        defer { StubServer.shared.unregister(host: host) }
        let feed = "https://feeds.example.com/rebuilt.xml"
        let guid = "rebuilt-episode-guid"

        // Instance "A": podcast 1, episode 10.
        let pageA = StubJSON.syncPage(
            instanceId: "A",
            podcasts: [StubJSON.podcast(id: 1, feedUrl: feed, title: "Rebuilt")],
            episodes: [StubJSON.episode(id: 10, podcastId: 1, guid: guid, title: "Pilot")],
            nextSince: 10,
            hasMore: false
        )
        StubServer.shared.register(host: host) { request, _ in
            guard request.url?.path == "/api/v1/sync" else { return .notFound }
            guard StubServer.queryValue("since", in: request) == "0" else {
                return .json(StubJSON.error(code: "invalidRequest", message: "Unexpected cursor"), status: 400)
            }
            return .json(pageA)
        }
        let harness = try SyncHarness(host: host)
        defer { harness.cleanUp() }

        await harness.service.syncNow(.manual)
        #expect(harness.service.lastError == nil)

        // Device-local state on the old row: a position and a queue entry.
        let main = harness.container.mainContext
        let fetchedOld = try storedEpisode(10, in: main)
        let oldEpisode = try #require(fetchedOld)
        oldEpisode.playbackPosition = 120
        oldEpisode.isPlayed = false
        main.insert(QueueItem(position: 0, episode: oldEpisode))
        try main.save()

        // The server database is recreated: instance "B", new ids, same
        // feed URL + guid. Any delta cursor from "A" gets a "B" page back.
        let pageB = StubJSON.syncPage(
            instanceId: "B",
            podcasts: [StubJSON.podcast(id: 5, feedUrl: feed, title: "Rebuilt")],
            episodes: [StubJSON.episode(id: 50, podcastId: 5, guid: guid, title: "Pilot")],
            nextSince: 60,
            hasMore: false
        )
        let deltaB = StubJSON.syncPage(instanceId: "B", nextSince: 30, hasMore: false)
        StubServer.shared.register(host: host) { request, _ in
            guard request.url?.path == "/api/v1/sync" else { return .notFound }
            let since = StubServer.queryValue("since", in: request) ?? "0"
            return since == "0" ? StubResponse.json(pageB) : StubResponse.json(deltaB)
        }
        let requestsBefore = StubServer.shared.requests(for: host).count

        await harness.service.syncNow(.manual)

        #expect(harness.service.lastError == nil)
        // The delta against "A" is discarded and the sync restarts from 0.
        let sinceValues = StubServer.shared.requests(for: host)
            .dropFirst(requestsBefore)
            .map { StubServer.queryValue("since", in: $0) }
        #expect(sinceValues == ["10", "0"])

        let context = ModelContext(harness.container)
        let podcastIDs = try context.fetch(FetchDescriptor<Podcast>()).map(\.serverID)
        let episodeIDs = try context.fetch(FetchDescriptor<Episode>()).map(\.serverID)
        #expect(podcastIDs == [5], "No row from instance A may survive")
        #expect(episodeIDs == [50], "No row from instance A may survive")

        let fetchedNew = try storedEpisode(50, in: context)
        let newEpisode = try #require(fetchedNew)
        #expect(newEpisode.guid == guid)
        #expect(
            newEpisode.playbackPosition == 120,
            "Re-attached by feedURL + guid from the snapshot taken before the wipe"
        )
        #expect(!newEpisode.isPlayed)
        let queueItems = try context.fetch(FetchDescriptor<QueueItem>())
        #expect(queueItems.count == 1)
        #expect(queueItems.first?.episode?.serverID == 50)

        let engine = try #require(harness.service.engine)
        let cursor = await engine.cursorState()
        #expect(cursor.instanceId == "B")
        #expect(cursor.nextSince == 60)
        #expect(harness.service.instanceId == "B")
        #expect(!harness.restore.hasPendingJobs)
    }

}
