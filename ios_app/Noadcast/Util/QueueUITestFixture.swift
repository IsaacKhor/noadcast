#if DEBUG
import Foundation
import SwiftData

/// An isolated queue for gesture tests; never opens the user's store or starts
/// sync/download services. Enabled only by the UI test launch argument.
enum QueueUITestFixture {
    static func makeContainer() -> ModelContainer {
        let schema = LocalStoreGeneration.schema
        let configuration = ModelConfiguration(schema: schema, isStoredInMemoryOnly: true)
        let container: ModelContainer
        do {
            container = try ModelContainer(for: schema, configurations: [configuration])
            let context = container.mainContext
            let settings = AppSettings.current(in: context)
            settings.autoDownloadPolicy = .manualOnly
            let podcast = Podcast(
                serverID: 1,
                feedURL: URL(string: "https://queue-test.invalid/feed.xml")!,
                title: "Queue test podcast"
            )
            context.insert(podcast)
            for number in 1...40 {
                let episode = Episode(
                    serverID: number,
                    podcastServerID: 1,
                    guid: "queue-test-\(number)",
                    title: String(format: "Queue episode %02d", number),
                    duration: 600,
                    podcast: podcast
                )
                context.insert(episode)
                context.insert(QueueItem(position: number - 1, episode: episode))
            }
            try context.save()
        } catch {
            fatalError("Could not create queue UI test fixture: \(error)")
        }
        return container
    }
}
#endif
