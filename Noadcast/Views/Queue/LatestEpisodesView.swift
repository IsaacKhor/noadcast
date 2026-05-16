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
                        LatestEpisodeRow(episode: episode)
                    }
                }
            }
        }
        .navigationTitle("Latest Episodes")
        .navigationBarTitleDisplayMode(.inline)
    }
}

private struct LatestEpisodeRow: View {
    @Environment(\.modelContext) private var context
    @Bindable var episode: Episode
    @State private var showNotes = false

    private let player = PlayerService.shared
    private let pipeline = ProcessingPipeline.shared

    var body: some View {
        HStack(spacing: 12) {
            Button {
                showNotes = true
            } label: {
                HStack(spacing: 12) {
                    AsyncImage(url: episode.podcast?.artworkDisplayURL) { phase in
                        switch phase {
                        case .success(let image): image.resizable()
                        default: Color.gray.opacity(0.2)
                        }
                    }
                    .frame(width: 44, height: 44)
                    .clipShape(RoundedRectangle(cornerRadius: 6))

                    VStack(alignment: .leading, spacing: 2) {
                        Text(episode.title)
                            .font(.subheadline.bold())
                            .lineLimit(2)
                        HStack(spacing: 4) {
                            Text(episode.podcast?.title ?? "")
                                .lineLimit(1)
                            if let date = episode.publishedAt {
                                Text("·")
                                Text(date.formatted(date: .abbreviated, time: .omitted))
                            }
                        }
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            actionButton
        }
        .swipeActions(edge: .leading) {
            Button {
                SubscriptionService.shared.addToQueue(episode, in: context)
            } label: {
                Label("Queue", systemImage: "text.badge.plus")
            }
            .tint(.blue)
        }
        .sheet(isPresented: $showNotes) {
            ShowNotesView(episode: episode)
        }
    }

    @ViewBuilder
    private var actionButton: some View {
        if episode.processingState == .ready, episode.hasLocalFile {
            Button {
                play()
            } label: {
                Image(systemName: "play.circle.fill").font(.title2)
            }
            .buttonStyle(.plain)
        } else if pipeline.isProcessing(episodeID: episode.persistentModelID) {
            ProgressView()
        } else {
            Button {
                pipeline.process(episode: episode)
            } label: {
                Image(systemName: "arrow.down.circle").font(.title2)
            }
            .buttonStyle(.plain)
        }
    }

    private func play() {
        let settings = AppSettings.current(in: context)
        player.load(episode: episode, settings: settings)
        player.play()
    }
}
