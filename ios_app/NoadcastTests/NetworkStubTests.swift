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

    @Test(arguments: ClassifierModel.allCases)
    func modelSelectionSendsOpenRouterAndExactModel(_ model: ClassifierModel) async throws {
        let host = StubServer.uniqueHost()
        defer { StubServer.shared.unregister(host: host) }
        let body = LockedBox<Data?>(nil)
        let response = #"{"adAnalysisEnabled":true,"autoProcessEnabled":true,"classifier":"openrouter","classifierModel":"\#(model.rawValue)","availableClassifiers":{"openrouter":true}}"#
        StubServer.shared.register(host: host) { request, _ in
            body.withLock { $0 = Self.requestBody(request) }
            return .json(response)
        }
        let client = try makeStubClient(host: host)

        let settings = try await client.updateSettings(classifierModel: model.rawValue)

        #expect(settings.classifierModel == model.rawValue)
        let request = try #require(StubServer.shared.requests(for: host).first)
        #expect(request.httpMethod == "PATCH")
        #expect(request.url?.path == "/api/v1/settings")
        let sent = try JSONDecoder().decode([String: String].self, from: #require(body.value))
        #expect(sent == ["classifier": "openrouter", "classifierModel": model.rawValue])
    }

    @Test func settingsPatchSendsFeedIntervalAndFlexFields() async throws {
        let host = StubServer.uniqueHost()
        defer { StubServer.shared.unregister(host: host) }
        let response = #"{"adAnalysisEnabled":true,"autoProcessEnabled":true,"feedIntervalMinutes":45,"openrouterFlexEnabled":true}"#
        StubServer.shared.register(host: host) { _, _ in .json(response) }
        let client = try makeStubClient(host: host)

        let settings = try await client.updateSettings(feedIntervalMinutes: 45, openrouterFlexEnabled: true)

        #expect(settings.feedIntervalMinutes == 45)
        #expect(settings.openrouterFlexEnabled == true)
        let request = try #require(StubServer.shared.requests(for: host).first)
        #expect(request.httpMethod == "PATCH")
        let requestData = try #require(Self.requestBody(request))
        let object = try JSONSerialization.jsonObject(with: requestData)
        let sent = try #require(object as? [String: Any])
        #expect(sent.count == 2)
        #expect(sent["feedIntervalMinutes"] as? Int == 45)
        #expect(sent["openrouterFlexEnabled"] as? Bool == true)
    }

    /// URLSession may move a request's body into a stream before URLProtocol sees it.
    private nonisolated static func requestBody(_ request: URLRequest) -> Data? {
        if let body = request.httpBody { return body }
        guard let stream = request.httpBodyStream else { return nil }
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 1024)
        while true {
            let count = stream.read(&buffer, maxLength: buffer.count)
            guard count > 0 else { break }
            data.append(contentsOf: buffer.prefix(count))
        }
        return data
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

    @Test @MainActor func acceptedModelSelectionPersistsTheServerResponse() async throws {
        let host = StubServer.uniqueHost()
        defer { StubServer.shared.unregister(host: host) }
        let response = #"{"adAnalysisEnabled":true,"autoProcessEnabled":true,"classifier":"openrouter","classifierModel":"qwen/qwen3.8-flash","availableClassifiers":{"openrouter":true}}"#
        let sync = StubJSON.syncPage(instanceId: "model-selection", nextSince: 1, hasMore: false)
        StubServer.shared.register(host: host) { request, _ in
            request.url?.path == "/api/v1/settings" ? .json(response) : .json(sync)
        }
        let harness = try SyncHarness(host: host)
        defer { harness.cleanUp() }
        let settings = AppSettings.current(in: harness.container.mainContext)
        settings.serverClassifierModel = ClassifierModel.deepSeekFlash.rawValue
        try harness.container.mainContext.save()

        try await harness.service.setClassifierModel(.qwenFlash)

        let persisted = AppSettings.current(in: ModelContext(harness.container))
        #expect(persisted.serverClassifierModel == ClassifierModel.qwenFlash.rawValue)
        #expect(persisted.serverClassifier == "openrouter")
        #expect(persisted.serverOpenRouterAvailable == true)
        #expect(harness.service.pendingClassifierModel == nil)
    }

    @Test @MainActor func rejectedModelSelectionPreservesTheStoredChoice() async throws {
        let host = StubServer.uniqueHost()
        defer { StubServer.shared.unregister(host: host) }
        StubServer.shared.register(host: host) { _, _ in
            .json(StubJSON.error(code: "invalidRequest", message: "Model unavailable"), status: 400)
        }
        let harness = try SyncHarness(host: host)
        defer { harness.cleanUp() }
        let settings = AppSettings.current(in: harness.container.mainContext)
        settings.serverClassifierModel = ClassifierModel.deepSeekFlash.rawValue
        try harness.container.mainContext.save()

        do {
            try await harness.service.setClassifierModel(.gptLunaHigh)
            Issue.record("Expected the rejected model change to throw")
        } catch let error as APIError {
            #expect(error == .invalidRequest(code: "invalidRequest", message: "Model unavailable"))
        }

        let persisted = AppSettings.current(in: ModelContext(harness.container))
        #expect(persisted.serverClassifierModel == ClassifierModel.deepSeekFlash.rawValue)
        #expect(harness.service.pendingClassifierModel == nil)
    }

    @Test @MainActor func settingsRefreshHydratesAndAcceptedPatchUsesServerValue() async throws {
        let host = StubServer.uniqueHost()
        defer { StubServer.shared.unregister(host: host) }
        let initial = #"{"adAnalysisEnabled":true,"autoProcessEnabled":true,"classifier":"openrouter","feedIntervalMinutes":30,"openrouterFlexEnabled":false}"#
        let accepted = #"{"adAnalysisEnabled":true,"autoProcessEnabled":true,"classifier":"openrouter","feedIntervalMinutes":45,"openrouterFlexEnabled":false}"#
        let sync = StubJSON.syncPage(instanceId: "settings-update", nextSince: 1, hasMore: false)
        StubServer.shared.register(host: host) { request, _ in
            guard request.url?.path == "/api/v1/settings" else { return .json(sync) }
            return .json(request.httpMethod == "PATCH" ? accepted : initial)
        }
        let harness = try SyncHarness(host: host)
        defer { harness.cleanUp() }

        #expect(!harness.service.hasLoadedServerSettings)
        try await harness.service.refreshServerSettings()
        #expect(harness.service.hasLoadedServerSettings)
        let settings = AppSettings.current(in: harness.container.mainContext)
        #expect(settings.serverFeedIntervalMinutes == 30)
        #expect(settings.serverOpenRouterFlexEnabled == false)
        try await harness.service.refreshServerSettings()
        #expect(!harness.container.mainContext.hasChanges)

        try await harness.service.setFeedIntervalMinutes(60)
        #expect(settings.serverFeedIntervalMinutes == 45)
        #expect(settings.serverOpenRouterFlexEnabled == false)
        #expect(!harness.service.isUpdatingServerSettings)
    }

    @Test @MainActor func rejectedFlexPatchPreservesMirror() async throws {
        let host = StubServer.uniqueHost()
        defer { StubServer.shared.unregister(host: host) }
        StubServer.shared.register(host: host) { _, _ in
            .json(StubJSON.error(code: "invalidRequest", message: "Flex unavailable"), status: 400)
        }
        let harness = try SyncHarness(host: host)
        defer { harness.cleanUp() }
        let settings = AppSettings.current(in: harness.container.mainContext)
        settings.serverOpenRouterFlexEnabled = false
        try harness.container.mainContext.save()

        do {
            try await harness.service.setOpenRouterFlexEnabled(true)
            Issue.record("Expected the rejected Flex change to throw")
        } catch let error as APIError {
            #expect(error == .invalidRequest(code: "invalidRequest", message: "Flex unavailable"))
        }

        #expect(settings.serverOpenRouterFlexEnabled == false)
        #expect(!harness.service.isUpdatingServerSettings)
    }

    @Test @MainActor func olderServerCannotSilentlyAcceptUnsupportedSetting() async throws {
        let host = StubServer.uniqueHost()
        defer { StubServer.shared.unregister(host: host) }
        StubServer.shared.register(host: host) { _, _ in .json(Self.settingsJSON) }
        let harness = try SyncHarness(host: host)
        defer { harness.cleanUp() }
        let settings = AppSettings.current(in: harness.container.mainContext)
        settings.serverFeedIntervalMinutes = 30
        try harness.container.mainContext.save()

        do {
            try await harness.service.setFeedIntervalMinutes(45)
            Issue.record("Expected an older server to report that the setting is unavailable")
        } catch let error as SyncError {
            #expect(error == .settingUnavailable("feed interval"))
        }
        #expect(settings.serverFeedIntervalMinutes == 30)
        #expect(!harness.service.isUpdatingServerSettings)

        do {
            try await harness.service.setFeedIntervalMinutes(0)
            Issue.record("Expected an out-of-range interval to be rejected locally")
        } catch let error as SyncError {
            #expect(error == .invalidFeedInterval)
        }
        #expect(StubServer.shared.requests(for: host).count == 1)
    }

    @Test @MainActor func settingsRefreshWaitsForInFlightPatch() async throws {
        let host = StubServer.uniqueHost()
        defer { StubServer.shared.unregister(host: host) }
        let gate = StubDeliveryGate()
        let response = #"{"adAnalysisEnabled":true,"autoProcessEnabled":true,"feedIntervalMinutes":30,"openrouterFlexEnabled":true}"#
        let sync = StubJSON.syncPage(instanceId: "settings-wait", nextSince: 1, hasMore: false)
        StubServer.shared.register(host: host) { request, _ in
            guard request.url?.path == "/api/v1/settings" else { return .json(sync) }
            return .json(response, deliveryGate: request.httpMethod == "PATCH" ? gate : nil)
        }
        let harness = try SyncHarness(host: host)
        defer { harness.cleanUp() }

        let patch = Task { try await harness.service.setOpenRouterFlexEnabled(true) }
        for _ in 0..<100 where !harness.service.isUpdatingServerSettings { await Task.yield() }
        #expect(harness.service.isUpdatingServerSettings)
        let refresh = Task { try await harness.service.refreshServerSettings() }
        await Task.yield()
        #expect(!harness.service.hasLoadedServerSettings)
        await gate.open()
        try await patch.value
        try await refresh.value
        #expect(harness.service.hasLoadedServerSettings)
        #expect(AppSettings.current(in: harness.container.mainContext).serverOpenRouterFlexEnabled == true)
    }

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
