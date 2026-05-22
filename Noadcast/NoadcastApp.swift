import SwiftUI
import SwiftData
import os

@main
struct NoadcastApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    let sharedModelContainer: ModelContainer = {
        Log.signposter.withIntervalSignpost("ModelContainer.init") {
            let schema = Schema([
                Podcast.self,
                Episode.self,
                TranscriptSegment.self,
                AdMarker.self,
                QueueItem.self,
                AppSettings.self
            ])
            let configuration = ModelConfiguration(schema: schema, isStoredInMemoryOnly: false)
            do {
                return try ModelContainer(for: schema, configurations: [configuration])
            } catch {
                fatalError("Could not create ModelContainer: \(error)")
            }
        }
    }()

    init() {
        Log.signposter.withIntervalSignpost("NoadcastApp.init") {
            Log.signposter.withIntervalSignpost("Wire.PlayerService") {
                PlayerService.shared.setModelContainer(sharedModelContainer)
            }
            Log.signposter.withIntervalSignpost("Wire.ProcessingPipeline") {
                ProcessingPipeline.shared.setModelContainer(sharedModelContainer)
            }
            Log.signposter.withIntervalSignpost("Wire.NetworkMonitor") {
                _ = NetworkMonitor.shared
            }
        }
        // Restart any cloud / download work that got interrupted by a
        // previous app termination. Runs after wiring so the pipeline
        // sees the model container.
        Task { await ProcessingPipeline.shared.recoverPendingEpisodes() }
    }

    var body: some Scene {
        WindowGroup {
            ContentView()
        }
        .modelContainer(sharedModelContainer)
    }
}
