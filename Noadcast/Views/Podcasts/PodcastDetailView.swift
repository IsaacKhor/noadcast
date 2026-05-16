import SwiftUI
import SwiftData

struct PodcastDetailView: View {
    @Environment(\.modelContext) private var context
    @Bindable var podcast: Podcast
    @Query private var settingsList: [AppSettings]
    @Query private var sortedEpisodes: [Episode]

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

    private var defaultSpeed: Double {
        settingsList.first?.defaultPlaybackSpeed ?? 1.0
    }

    var body: some View {
        List {
            Section {
                HStack(spacing: 12) {
                    AsyncImage(url: podcast.artworkDisplayURL) { phase in
                        switch phase {
                        case .success(let image): image.resizable()
                        default: Color.gray.opacity(0.2)
                        }
                    }
                    .frame(width: 80, height: 80)
                    .clipShape(RoundedRectangle(cornerRadius: 8))

                    VStack(alignment: .leading) {
                        Text(podcast.author ?? "").font(.caption).foregroundStyle(.secondary)
                        Text("\(sortedEpisodes.count) episodes")
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    }
                }
                if let summary = podcast.summary, !summary.isEmpty {
                    Text(summary).font(.subheadline)
                }
            }

            if let lastFetched = podcast.lastFetched {
                Section {
                    HStack {
                        Label("Last refresh", systemImage: "arrow.clockwise")
                            .foregroundStyle(.secondary)
                        Spacer()
                        Text(TimeFormatting.refreshTimestamp(lastFetched))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }

            Section {
                Toggle("Auto-download new episodes", isOn: $podcast.autoDownloadEnabled)
                Toggle("Detect & skip ads", isOn: $podcast.aiProcessingEnabled)
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
            } header: {
                Text("Settings")
            } footer: {
                Text("When ad detection is off, this podcast's new episodes are downloaded but not transcribed, and no ads will be marked or skipped during playback.")
            }

            Section("Episodes") {
                ForEach(sortedEpisodes) { episode in
                    EpisodeRow(episode: episode, style: .episodeOnly)
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
        .navigationTitle(podcast.title)
        .navigationBarTitleDisplayMode(.inline)
        .refreshable {
            try? await SubscriptionService.shared.refresh(podcast: podcast, in: context)
        }
    }

    private var speedBinding: Binding<Double?> {
        Binding(
            get: { podcast.customPlaybackSpeed },
            set: { podcast.customPlaybackSpeed = $0 }
        )
    }
}

