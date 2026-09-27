#if DEBUG
import Foundation
import SwiftData

/// Exercises the real refresh controls without starting any production services.
enum PodcastsUITestFixture {
    static func makeContainer() -> ModelContainer {
        let schema = LocalStoreGeneration.schema
        let configuration = ModelConfiguration(schema: schema, isStoredInMemoryOnly: true)
        do {
            let container = try ModelContainer(for: schema, configurations: [configuration])
            container.mainContext.insert(Podcast(
                serverID: 9_200_001,
                feedURL: URL(string: "https://refresh-ui-test.invalid/feed.xml")!,
                title: "Refresh fixture"
            ))
            try container.mainContext.save()
            return container
        } catch {
            fatalError("Could not create refresh UI fixture: \(error)")
        }
    }

    static func refresh(context: ModelContext) async throws {
        try await Task.sleep(for: .milliseconds(300))
        if ProcessInfo.processInfo.arguments.contains("--refresh-fails") {
            throw URLError(.notConnectedToInternet)
        }
        AppSettings.current(in: context).lastGlobalRefreshAt = .now
        try context.save()
    }
}
#endif
