import SwiftUI
import SwiftData

struct PodcastsView: View {
    @Environment(\.modelContext) private var context
    @Query private var podcasts: [Podcast]
    @Query private var settingsList: [AppSettings]
    @State private var showAdd = false

    private var settings: AppSettings? { settingsList.first }
    private var sortMode: PodcastSortMode { settings?.podcastSortMode ?? .latestEpisode }

    private var sortedPodcasts: [Podcast] {
        switch sortMode {
        case .alphabetical:
            return podcasts.sorted {
                $0.title.localizedCaseInsensitiveCompare($1.title) == .orderedAscending
            }
        case .latestEpisode:
            return podcasts.sorted {
                $0.latestEpisodeSortDate > $1.latestEpisodeSortDate
            }
        }
    }

    var body: some View {
        NavigationStack {
            Group {
                if podcasts.isEmpty {
                    ContentUnavailableView {
                        Label("No podcasts yet", systemImage: "rectangle.stack.badge.plus")
                    } description: {
                        Text("Tap + to add a podcast by feed URL or search the iTunes directory.")
                    }
                } else {
                    List {
                        if let lastRefresh = settings?.lastGlobalRefreshAt {
                            Section {
                                HStack {
                                    Label("Last refresh", systemImage: "arrow.clockwise")
                                        .foregroundStyle(.secondary)
                                    Spacer()
                                    Text(TimeFormatting.refreshTimestamp(lastRefresh))
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                            }
                        }
                        Section {
                            ForEach(sortedPodcasts) { podcast in
                                NavigationLink {
                                    PodcastDetailView(podcast: podcast)
                                } label: {
                                    PodcastRow(podcast: podcast)
                                }
                            }
                            .onDelete(perform: deletePodcasts)
                        }
                    }
                }
            }
            .navigationTitle("Podcasts")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) { sortMenu }
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        showAdd = true
                    } label: {
                        Image(systemName: "plus")
                    }
                }
            }
            .sheet(isPresented: $showAdd) {
                AddPodcastView()
            }
            .refreshable {
                await SubscriptionService.shared.refreshAll(context: context)
            }
        }
    }

    private var sortMenu: some View {
        Menu {
            ForEach(PodcastSortMode.allCases, id: \.self) { mode in
                Button {
                    settings?.podcastSortMode = mode
                    try? context.save()
                } label: {
                    if mode == sortMode {
                        Label(mode.label, systemImage: "checkmark")
                    } else {
                        Text(mode.label)
                    }
                }
            }
        } label: {
            Image(systemName: "arrow.up.arrow.down")
        }
    }

    private func deletePodcasts(at offsets: IndexSet) {
        let toDelete = offsets.map { sortedPodcasts[$0] }
        for podcast in toDelete {
            try? SubscriptionService.shared.unsubscribe(podcast, in: context)
        }
    }
}

private struct PodcastRow: View {
    let podcast: Podcast

    var body: some View {
        HStack(spacing: 12) {
            AsyncImage(url: podcast.artworkURL) { phase in
                switch phase {
                case .success(let image): image.resizable()
                default: Color.gray.opacity(0.2)
                }
            }
            .frame(width: 56, height: 56)
            .clipShape(RoundedRectangle(cornerRadius: 8))

            VStack(alignment: .leading, spacing: 2) {
                Text(podcast.title).font(.headline).lineLimit(2)
                if let author = podcast.author {
                    Text(author).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
                HStack(spacing: 6) {
                    Text("\(podcast.episodes.count) episodes")
                    if let latest = podcast.latestEpisodePublishedAt {
                        Text("·")
                        Text(latest, format: .relative(presentation: .named))
                    }
                }
                .font(.caption2)
                .foregroundStyle(.tertiary)
            }
        }
    }
}

extension Podcast {
    /// Cached "newest episode" date. Reads only the scalar field on the
    /// `Podcast` row — no relationship traversal. See `Podcast.latestEpisodeAt`
    /// for how the cache is maintained.
    var latestEpisodePublishedAt: Date? {
        latestEpisodeAt
    }

    /// Sort key: falls back to `dateAdded` so feeds with un-dated entries
    /// don't sink to the bottom forever.
    var latestEpisodeSortDate: Date {
        latestEpisodeAt ?? dateAdded
    }
}
