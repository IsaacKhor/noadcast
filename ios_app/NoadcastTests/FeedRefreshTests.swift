import Testing
import SwiftData
import Foundation
@testable import Noadcast

extension NetworkStubTests {
    @Test func refreshAllRequestsAnImmediateServerFeedFetch() async throws {
        let host = StubServer.uniqueHost()
        defer { StubServer.shared.unregister(host: host) }
        StubServer.shared.register(host: host) { _, _ in .json("{}", status: 202) }
        let client = try makeStubClient(host: host)
        try await client.refreshAll()
        let requests = StubServer.shared.requests(for: host)
        #expect(requests.count == 1)
        #expect(requests.first?.httpMethod == "POST")
        #expect(requests.first?.url?.path == "/api/v1/refresh")
    }
}

struct FeedIntervalTests {
    @Test func intervalDecodesAndOlderServersRemainCompatible() throws {
        let decoder = APIJSON.makeDecoder()
        let current = try decoder.decode(ServerSettingsDTO.self, from: Data(#"{"feedIntervalMinutes":45}"#.utf8))
        #expect(current.feedIntervalMinutes == 45)
        let older = try decoder.decode(ServerSettingsDTO.self, from: Data("{}".utf8))
        #expect(older.feedIntervalMinutes == nil)
    }

    @Test @MainActor func intervalMirrorIsWriteIfChanged() async throws {
        let container = try makeTestContainer()
        let engine = SyncEngine(modelContainer: container)
        let page = SyncFixtures.page(settings: ServerSettingsDTO(feedIntervalMinutes: 45), nextSince: 10)
        _ = try await engine.apply(page: page, options: SyncFixtures.options)
        #expect(AppSettings.current(in: ModelContext(container)).serverFeedIntervalMinutes == 45)
        let repeated = try await engine.apply(page: page, options: SyncFixtures.options)
        #expect(!repeated.settingsChanged)
        #expect(await !engine.hasUnsavedChanges())
    }
}
