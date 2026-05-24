import SwiftUI
import SwiftData
import AVKit

struct NowPlayingView: View {
    @Environment(\.modelContext) private var context
    @Query private var settingsList: [AppSettings]

    private var player = PlayerService.shared

    @State private var showNotes = false
    @State private var showAds = false

    private var settings: AppSettings? { settingsList.first }
    private var globalAdSkippingEnabled: Bool { settings?.skipAds == true }

    /// Direct lookup by the player's `PersistentIdentifier`. Avoids the
    /// previous fetch-all-Episodes-and-`.first` pattern which faulted every
    /// `Episode` in the store on each render. The episode is already loaded
    /// in the context (PlayerService put it there), so this is an O(1) cache
    /// hit; @Observable propagates property changes from there.
    private var currentEpisode: Episode? {
        guard let id = player.currentEpisodeID else { return nil }
        return context.model(for: id) as? Episode
    }

    var body: some View {
        NavigationStack {
            Group {
                if let episode = currentEpisode {
                    nowPlayingContent(for: episode)
                } else {
                    emptyState
                }
            }
            .navigationTitle("Now Playing")
            .navigationBarTitleDisplayMode(.inline)
        }
        .onAppear {
            syncAdSkipSetting()
        }
        .onChange(of: settings?.skipAds) { _, _ in
            syncAdSkipSetting()
        }
        .sheet(isPresented: $showNotes) {
            if let ep = currentEpisode {
                ShowNotesView(episode: ep)
            }
        }
        .sheet(isPresented: $showAds) {
            if let ep = currentEpisode {
                AdsTranscriptView(
                    ads: ep.adMarkers,
                    onSeek: { time in
                        player.seek(to: time)
                        showAds = false
                    }
                )
            }
        }
    }

    @ViewBuilder
    private func nowPlayingContent(for episode: Episode) -> some View {
        ScrollView {
            VStack(spacing: 24) {
                artwork(for: episode)

                VStack(spacing: 4) {
                    Text(episode.title)
                        .font(.headline)
                        .multilineTextAlignment(.center)
                    Text(episode.podcastTitle ?? episode.podcast?.title ?? "")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }

                AdMarkerTimeline(
                    currentTime: player.currentTime,
                    duration: player.duration,
                    adRegions: player.adRegions,
                    onSeek: { player.seek(to: $0) }
                )

                adSummary

                transportControls

                playbackOptions

                HStack(spacing: 16) {
                    Button {
                        showNotes = true
                    } label: {
                        Label("Show Notes", systemImage: "doc.text")
                    }

                    AudioOutputRoutePickerButton()
                        .frame(width: 44, height: 36)
                        .background(.quaternary, in: RoundedRectangle(cornerRadius: 8))
                        .accessibilityLabel("Audio Output")
                }
                .buttonStyle(.bordered)
            }
            .padding()
        }
    }

    private func artwork(for episode: Episode) -> some View {
        let url = episode.podcastArtworkDisplayURL ?? episode.podcast?.artworkDisplayURL
        return AsyncImage(url: url) { phase in
            switch phase {
            case .success(let image):
                image.resizable().aspectRatio(contentMode: .fit)
            default:
                RoundedRectangle(cornerRadius: 16)
                    .fill(.quaternary)
                    .overlay(
                        Image(systemName: "waveform")
                            .font(.system(size: 64))
                            .foregroundStyle(.secondary)
                    )
            }
        }
        .frame(maxWidth: 280, maxHeight: 280)
        .clipShape(RoundedRectangle(cornerRadius: 16))
    }

    private var adSummary: some View {
        Button {
            showAds = true
        } label: {
            HStack(spacing: 16) {
                Image(systemName: "speaker.slash.fill")
                    .foregroundStyle(.orange)
                VStack(alignment: .leading) {
                    Text(detectionSummary)
                        .font(.subheadline.bold())
                    Text("\(player.skippedAds) skipped this session")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Image(systemName: "chevron.right")
                    .font(.caption.bold())
                    .foregroundStyle(.tertiary)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .background(.orange.opacity(0.1), in: RoundedRectangle(cornerRadius: 10))
        }
        .buttonStyle(.plain)
        .disabled(player.adRegions.isEmpty)
    }

    /// Headline of the segments pill, e.g. "3 ads · intro · outro".
    private var detectionSummary: String {
        let adCount = player.adRegions.filter { $0.kind == .ad }.count
        let hasIntro = player.adRegions.contains { $0.kind == .intro }
        let hasOutro = player.adRegions.contains { $0.kind == .outro }
        var parts: [String] = []
        if adCount > 0 { parts.append("\(adCount) ad\(adCount == 1 ? "" : "s")") }
        if hasIntro { parts.append("intro") }
        if hasOutro { parts.append("outro") }
        if parts.isEmpty { return "Nothing to skip" }
        return parts.joined(separator: " · ")
    }

    private var transportControls: some View {
        HStack(spacing: 36) {
            Button { player.skipBackward(15) } label: {
                Image(systemName: "gobackward.15").font(.title)
            }
            Button {
                player.togglePlayPause()
            } label: {
                Image(systemName: player.isPlaying ? "pause.circle.fill" : "play.circle.fill")
                    .font(.system(size: 64))
            }
            Button { player.skipForward(30) } label: {
                Image(systemName: "goforward.30").font(.title)
            }
        }
    }

    private var playbackOptions: some View {
        VStack(spacing: 12) {
            HStack {
                Text("Speed")
                    .font(.subheadline)
                Spacer()
                Picker("Speed", selection: Binding(
                    get: { player.playbackRate },
                    set: { player.setPlaybackRate($0) }
                )) {
                    ForEach(PlaybackSpeed.options, id: \.self) { rate in
                        Text(PlaybackSpeed.label(for: rate)).tag(rate)
                    }
                }
                .pickerStyle(.menu)
            }

            if globalAdSkippingEnabled, player.adRegions.contains(where: { $0.kind == .ad }) {
                Toggle("Play ads this episode", isOn: Binding(
                    get: { player.playAdsForCurrentEpisode },
                    set: { player.setPlayAdsForCurrentEpisode($0) }
                ))
            }
        }
        .padding(.horizontal)
    }

    private func syncAdSkipSetting() {
        guard let settings else { return }
        player.setSkipAdsEnabled(settings.skipAds)
    }

    private var emptyState: some View {
        VStack(spacing: 12) {
            Image(systemName: "play.circle")
                .font(.system(size: 80))
                .foregroundStyle(.secondary)
            Text("Nothing playing")
                .font(.headline)
            Text("Pick an episode from the Queue or Podcasts tab.")
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 40)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

private struct AudioOutputRoutePickerButton: UIViewRepresentable {
    func makeUIView(context: Context) -> AVRoutePickerView {
        let view = AVRoutePickerView()
        configure(view)
        return view
    }

    func updateUIView(_ uiView: AVRoutePickerView, context: Context) {
        configure(uiView)
    }

    private func configure(_ view: AVRoutePickerView) {
        view.prioritizesVideoDevices = false
        view.tintColor = .label
        view.activeTintColor = .systemBlue
        view.backgroundColor = .clear
        view.accessibilityLabel = "Audio Output"
    }
}
