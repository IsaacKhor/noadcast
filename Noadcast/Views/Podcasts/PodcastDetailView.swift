import SwiftUI
import SwiftData

struct PodcastDetailView: View {
    @Environment(\.modelContext) private var context
    @Bindable var podcast: Podcast
    @Query private var sortedEpisodes: [Episode]
    @State private var defaultSpeed: Double = 1.0

    init(podcast: Podcast) {
        self.podcast = podcast
        // SwiftData translates this predicate to a SQL `WHERE` + `ORDER BY`,
        // so a 500-episode archive doesn't fault every row's `publishedAt`
        // through main-thread accessors just to sort the list.
        let feedURL = podcast.feedURL
        self._sortedEpisodes = Query(
            filter: #Predicate<Episode> { $0.podcast?.feedURL == feedURL },
            sort: \Episode.publishedAt,
            order: .reverse
        )
    }

    var body: some View {
        List {
            // Podcast header — artwork + author + episode count. Sits in
            // the same list as the episodes so the whole thing scrolls
            // together (matches Pocket Casts / Overcast).
            Section {
                VStack(alignment: .leading, spacing: 12) {
                    HStack(spacing: 12) {
                        CachedArtworkImage(url: podcast.cachedArtworkDisplayURL, size: 80)
                            .frame(width: 80, height: 80)
                            .clipShape(RoundedRectangle(cornerRadius: 8))

                        VStack(alignment: .leading, spacing: 2) {
                            if let author = podcast.author, !author.isEmpty {
                                Text(author).font(.caption).foregroundStyle(.secondary)
                            }
                            Text("\(podcast.episodeCount) episodes")
                                .font(.caption2)
                                .foregroundStyle(.tertiary)
                            if let lastFetched = podcast.lastFetched {
                                Text("Refreshed \(TimeFormatting.refreshTimestamp(lastFetched))")
                                    .font(.caption2)
                                    .foregroundStyle(.tertiary)
                            }
                        }
                        Spacer(minLength: 0)
                    }
                    if let summary = podcast.summary, !summary.isEmpty {
                        Text(summary).font(.subheadline).foregroundStyle(.secondary)
                    }
                }
                .listRowInsets(.init(top: 16, leading: 16, bottom: 12, trailing: 16))
            }

            Section {
                Toggle("Auto-download new episodes", isOn: $podcast.autoDownloadEnabled)
                    .listRowInsets(.init(top: 6, leading: 16, bottom: 6, trailing: 16))
                Toggle("Detect & skip ads", isOn: $podcast.aiProcessingEnabled)
                    .listRowInsets(.init(top: 6, leading: 16, bottom: 6, trailing: 16))
                HStack {
                    Text("Playback speed")
                    Spacer()
                    Picker("Speed", selection: speedBinding) {
                        Text("Default (\(PlaybackSpeed.label(for: defaultSpeed)))").tag(Double?.none)
                        ForEach(PlaybackSpeed.options, id: \.self) { rate in
                            Text(PlaybackSpeed.label(for: rate)).tag(Optional(rate))
                        }
                    }
                    .pickerStyle(.menu)
                }
                .listRowInsets(.init(top: 6, leading: 16, bottom: 6, trailing: 16))
            } header: {
                Text("Settings")
            } footer: {
                Text("When ad detection is off, this podcast's new episodes are downloaded but not analyzed, and no ads will be marked or skipped during playback.")
            }

            Section("Episodes") {
                ForEach(sortedEpisodes) { episode in
                    EpisodeRow(episode: episode, style: .episodeOnly)
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
        }
        .listStyle(.plain)
        .navigationTitle(podcast.title)
        .navigationBarTitleDisplayMode(.inline)
        .onAppear { refreshSettingsSnapshot() }
        .refreshable {
            try? await SubscriptionService.shared.refresh(podcast: podcast, in: context)
            refreshSettingsSnapshot()
        }
    }

    private func refreshSettingsSnapshot() {
        defaultSpeed = AppSettings.current(in: context).defaultPlaybackSpeed
    }

    private var speedBinding: Binding<Double?> {
        Binding(
            get: { podcast.customPlaybackSpeed },
            set: { podcast.customPlaybackSpeed = $0 }
        )
    }
}
