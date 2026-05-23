import SwiftUI
import SwiftData

struct PodcastsView: View {
    @Environment(\.modelContext) private var context
    /// Sorted at the SwiftData layer — body never calls `.sorted(by:)`.
    /// The alphabetical mode is handled by maintaining a `@State` cache
    /// refreshed only when the data or the mode actually changes
    /// (`refreshSort()`); a body re-eval has no per-frame sort cost.
    @Query(sort: \Podcast.latestEpisodeAt, order: .reverse) private var podcasts: [Podcast]
    @State private var showAdd = false
    @State private var searchText = ""
    @State private var sortedPodcasts: [Podcast] = []
    @State private var sortMode: PodcastSortMode = .latestEpisode
    @State private var lastGlobalRefreshAt: Date?

    private var visiblePodcasts: [Podcast] {
        let source = sortedPodcasts.isEmpty && !podcasts.isEmpty ? podcasts : sortedPodcasts
        let trimmed = searchText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return source }
        return source.filter { podcast in
            podcast.title.localizedCaseInsensitiveContains(trimmed) ||
            (podcast.author?.localizedCaseInsensitiveContains(trimmed) ?? false)
        }
    }

    private func refreshSort() {
        switch sortMode {
        case .latestEpisode:
            // `@Query` already returns the rows in this order.
            sortedPodcasts = podcasts
        case .alphabetical:
            sortedPodcasts = podcasts.sorted {
                $0.title.localizedCaseInsensitiveCompare($1.title) == .orderedAscending
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
                } else if visiblePodcasts.isEmpty {
                    ContentUnavailableView.search(text: searchText)
                } else {
                    List {
                        if let lastRefresh = lastGlobalRefreshAt, searchText.isEmpty {
                            Section {
                                HStack {
                                    Label("Last refresh", systemImage: "arrow.clockwise")
                                        .foregroundStyle(.secondary)
                                    Spacer()
                                    Text(TimeFormatting.refreshTimestamp(lastRefresh))
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                                .listRowInsets(.init(top: 10, leading: 16, bottom: 10, trailing: 16))
                            }
                        }
                        Section {
                            ForEach(visiblePodcasts) { podcast in
                                NavigationLink {
                                    PodcastDetailView(podcast: podcast)
                                } label: {
                                    PodcastRow(podcast: podcast)
                                }
                                .listRowInsets(.init(top: 8, leading: 16, bottom: 8, trailing: 16))
                            }
                            .onDelete(perform: deletePodcasts)
                        }
                    }
                    .listStyle(.plain)
                }
            }
            .navigationTitle("Podcasts")
            .navigationBarTitleDisplayMode(.inline)
            .searchable(text: $searchText, prompt: "Search podcasts")
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
                refreshSettingsSnapshot()
            }
            .onAppear { refreshSettingsSnapshot() }
            .onChange(of: podcasts) { _, _ in refreshSort() }
            .onChange(of: sortMode) { _, _ in refreshSort() }
        }
    }

    private func refreshSettingsSnapshot() {
        let settings = AppSettings.current(in: context)
        sortMode = settings.podcastSortMode
        lastGlobalRefreshAt = settings.lastGlobalRefreshAt
        refreshSort()
    }

    private var sortMenu: some View {
        Menu {
            ForEach(PodcastSortMode.allCases, id: \.self) { mode in
                Button {
                    let settings = AppSettings.current(in: context)
                    settings.podcastSortMode = mode
                    sortMode = mode
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
        let toDelete = offsets.map { visiblePodcasts[$0] }
        for podcast in toDelete {
            try? SubscriptionService.shared.unsubscribe(podcast, in: context)
        }
    }
}

private struct PodcastRow: View {
    let podcast: Podcast

    var body: some View {
        HStack(spacing: 12) {
            CachedArtworkImage(url: podcast.cachedArtworkDisplayURL, size: 56)
                .frame(width: 56, height: 56)
                .clipShape(RoundedRectangle(cornerRadius: 8))

            VStack(alignment: .leading, spacing: 2) {
                Text(podcast.title).font(.headline).lineLimit(2)
                if let author = podcast.author {
                    Text(author).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
                HStack(spacing: 6) {
                    Text("\(podcast.episodeCount) episodes")
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
