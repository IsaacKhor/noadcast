#if DEBUG
import Foundation
import SwiftData

/// An isolated queue for gesture tests; never opens the user's store or starts
/// sync/download services. Enabled only by the UI test launch argument.
enum QueueUITestFixture {
    private static let suiteName = "Noadcast.QueueUITestFixture"
    private static let defaults = UserDefaults(suiteName: suiteName)!
    static let audioFilename = "queue-ui-test-\(UUID().uuidString).mp3"
    static let subscription = SubscriptionService(
        releasePlayedAudio: { PendingReleaseStore.add($0, instanceId: "queue-ui-test", defaults: defaults) },
        cancelPendingRelease: { PendingReleaseStore.remove($0, defaults: defaults) },
        cancelLocalTransfer: { _ in },
        cancelServerAudioRequest: { _ in }
    )
    static var releasePending: Bool {
        PendingReleaseStore.load(defaults: defaults).ids.contains(9_100_002)
    }

    static func makeContainer() -> ModelContainer {
        defaults.removePersistentDomain(forName: suiteName)
        let schema = LocalStoreGeneration.schema
        let configuration = ModelConfiguration(schema: schema, isStoredInMemoryOnly: true)
        let container: ModelContainer
        do {
            container = try ModelContainer(for: schema, configurations: [configuration])
            let context = container.mainContext
            let settings = AppSettings.current(in: context)
            settings.autoDownloadPolicy = .manualOnly
            for filename in AudioStorage.listEpisodeFiles() where filename.hasPrefix("queue-ui-test-") {
                AudioStorage.deleteFile(named: filename)
            }
            try Data(repeating: 0, count: 16).write(to: AudioStorage.fileURL(for: audioFilename))
            let podcast = Podcast(
                serverID: 9_100_000,
                feedURL: URL(string: "https://queue-test.invalid/feed.xml")!,
                title: "Queue test podcast"
            )
            context.insert(podcast)
            for number in 1...40 {
                let episode = Episode(
                    serverID: 9_100_000 + number,
                    podcastServerID: podcast.serverID,
                    guid: "queue-test-\(number)",
                    title: String(format: "Queue episode %02d", number),
                    duration: 600,
                    podcast: podcast
                )
                if number == 2 {
                    episode.localFilename = audioFilename
                    episode.fileSizeBytes = 16
                    episode.setDownloadState(.downloaded)
                }
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
