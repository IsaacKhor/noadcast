import SwiftUI
import SwiftData

/// Cross-podcast view of the most recent 100 episodes in the library,
/// sorted by `publishedAt` descending. Accessible from the Queue tab's
/// toolbar.
struct LatestEpisodesView: View {
    @Environment(\.modelContext) private var context
    @Query private var episodes: [Episode]

    init() {
        var descriptor = FetchDescriptor<Episode>(
            sortBy: [SortDescriptor(\Episode.publishedAt, order: .reverse)]
        )
        descriptor.fetchLimit = 100
        self._episodes = Query(descriptor)
    }

    var body: some View {
        Group {
            if episodes.isEmpty {
                ContentUnavailableView {
                    Label("No episodes yet", systemImage: "clock.arrow.circlepath")
                } description: {
                    Text("Subscribe to a podcast to see its latest episodes here.")
                }
            } else {
                List {
                    ForEach(episodes) { episode in
                        EpisodeRow(episode: episode, style: .withPodcast)
                            .listRowInsets(.init(top: 8, leading: 16, bottom: 8, trailing: 16))
                            .swipeActions(edge: .leading) {
                                Button {
                                    SubscriptionService.shared.addToQueue(episode, in: context)
                                } label: {
                                    Label("Queue", systemImage: "text.badge.plus")
                                }
                                .tint(.blue)
                            }
                    }
                }
                .listStyle(.plain)
            }
        }
        .navigationTitle("Latest Episodes")
        .navigationBarTitleDisplayMode(.inline)
    }
}
