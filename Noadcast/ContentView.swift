import SwiftUI
import SwiftData
import os

struct ContentView: View {
    @Environment(\.modelContext) private var context
    @State private var showFullPlayer = false

    private let player = PlayerService.shared

    var body: some View {
        TabView {
            Tab("Queue", systemImage: "list.bullet") {
                QueueView()
            }
            Tab("Podcasts", systemImage: "rectangle.stack.fill") {
                PodcastsView()
            }
            Tab("Downloads", systemImage: "arrow.down.circle") {
                DownloadsView()
            }
            Tab("Settings", systemImage: "gear") {
                SettingsView()
            }
        }
        .tabViewBottomAccessory {
            MiniPlayerBar(onTap: {
                if player.currentEpisodeID != nil {
                    showFullPlayer = true
                }
            })
        }
        .sheet(isPresented: $showFullPlayer) {
            NowPlayingView()
        }
        .task {
            let taskState = Log.signposter.beginInterval("ContentView.task")
            defer { Log.signposter.endInterval("ContentView.task", taskState) }
            Log.signposter.withIntervalSignpost("AppSettings.current") {
                _ = AppSettings.current(in: context)
            }
            PlayerService.shared.restoreLastPlayedEpisode(context: context)

            // Backfill cached Podcast.latestEpisodeAt for any rows that
            // pre-date the field. Runs on a background `@ModelActor` so the
            // expensive episode-relationship walk doesn't block the UI.
            let container = context.container
            Task.detached(priority: .background) {
                let backfiller = MetadataBackfillActor(modelContainer: container)
                await backfiller.backfillLatestEpisodeDates()
            }

            // Backfill artwork for podcasts that were subscribed before the
            // local-cache feature shipped (or whose previous cache attempt
            // failed). `cache(for:)` is a no-op when the file is already on
            // disk, so this is cheap to call against the whole library.
            Task {
                await ArtworkService.shared.backfillAllPodcasts(context: context)
            }
        }
    }
}

#Preview {
    ContentView()
        .modelContainer(for: [
            Podcast.self, Episode.self, AdMarker.self,
            TranscriptSegment.self, QueueItem.self, AppSettings.self
        ], inMemory: true)
}
