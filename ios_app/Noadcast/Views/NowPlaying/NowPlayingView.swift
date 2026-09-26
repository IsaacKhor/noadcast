import SwiftUI
import SwiftData
import AVKit

struct NowPlayingView: View {
    @Environment(\.modelContext) private var context
    @Query private var settingsList: [AppSettings]

    private var player = PlayerService.shared

    @State private var showNotes = false
    @State private var showAds = false
    @State private var selectedPage = 0

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
                    TabView(selection: $selectedPage) {
                        nowPlayingContent(for: episode)
                            .tag(0)
                        ChaptersView(
                            chapters: player.chapters,
                            currentTime: player.currentTime,
                            isLoading: player.isLoadingChapters,
                            hasAudioItem: player.sourceKind != .none
                        ) {
                            player.seek(to: $0)
                        }
                        .tag(1)
                    }
                    .tabViewStyle(.page(indexDisplayMode: .never))
                    .onChange(of: episode.serverID) { _, _ in selectedPage = 0 }
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
                SkipSegmentsView(
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
        GeometryReader { geometry in
            let wide = geometry.size.width > geometry.size.height * 1.15
            let compact = geometry.size.height < 600
            if wide {
                HStack(spacing: 20) {
                    VStack(spacing: 8) {
                        artwork(for: episode, size: min(100, geometry.size.height * 0.24))
                        episodeHeading(for: episode)
                        adSummary(for: episode)
                        playbackStatus(compact: true)
                    }
                    .frame(width: min(200, geometry.size.width * 0.32))
                    playerControls(for: episode, compact: true, showAdSummary: false, showStatus: false)
                        .frame(maxWidth: .infinity)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .padding(.horizontal, 16)
            } else {
                VStack(spacing: compact ? 4 : 9) {
                    Spacer(minLength: 0)
                    artwork(for: episode, size: min(compact ? 90 : 140, geometry.size.height * 0.16))
                    episodeHeading(for: episode)
                    playerControls(for: episode, compact: compact)
                    Spacer(minLength: 0)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .padding(.horizontal, 16)
            }
        }
    }

    private func episodeHeading(for episode: Episode) -> some View {
        VStack(spacing: 4) {
            Text(episode.title)
                .font(.headline)
                .multilineTextAlignment(.center)
                .lineLimit(2)
            Text(episode.podcastTitle ?? episode.podcast?.title ?? "")
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .lineLimit(1)
        }
    }

    private func playerControls(
        for episode: Episode,
        compact: Bool,
        showAdSummary: Bool = true,
        showStatus: Bool = true
    ) -> some View {
        VStack(spacing: compact ? 4 : 9) {
            AdMarkerTimeline(
                currentTime: player.currentTime,
                duration: player.duration,
                adRegions: visibleAdRegions(for: episode),
                bufferedRanges: player.bufferedRanges,
                onSeek: { player.seek(to: $0) }
            )
            if showStatus { playbackStatus(compact: compact) }
            if showAdSummary { adSummary(for: episode) }
            transportControls
            playbackOptions
            HStack(spacing: 12) {
                Button { showNotes = true } label: {
                    Label(compact ? "Notes" : "Show Notes", systemImage: "doc.text")
                }
                Button { selectedPage = 1 } label: {
                    Label("Chapters", systemImage: "list.bullet")
                }
                AudioOutputRoutePickerButton()
                    .frame(width: 44, height: 44)
                    .background(.quaternary, in: RoundedRectangle(cornerRadius: 8))
                    .accessibilityLabel("Audio Output")
            }
            .buttonStyle(.bordered)
        }
        .frame(maxWidth: .infinity)
    }

    private func artwork(for episode: Episode, size: CGFloat) -> some View {
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
        .frame(width: size, height: size)
        .clipShape(RoundedRectangle(cornerRadius: 16))
    }

    private func adSummary(for episode: Episode) -> some View {
        let markers = episode.adMarkers.filter { !$0.isDeleted }
        return Button {
            showAds = true
        } label: {
            HStack(spacing: 16) {
                Image(systemName: "speaker.slash.fill")
                    .foregroundStyle(.orange)
                VStack(alignment: .leading) {
                    Text(detectionSummary(for: markers))
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
        .disabled(markers.isEmpty)
    }

    /// Headline of the segments pill, e.g. "3 ads · intro · outro".
    private func detectionSummary(for markers: [AdMarker]) -> String {
        let adCount = markers.filter { $0.kind == .ad }.count
        let hasIntro = markers.contains { $0.kind == .intro }
        let hasOutro = markers.contains { $0.kind == .outro }
        var parts: [String] = []
        if adCount > 0 { parts.append("\(adCount) ad\(adCount == 1 ? "" : "s")") }
        if hasIntro { parts.append("intro") }
        if hasOutro { parts.append("outro") }
        if parts.isEmpty { return "Nothing to skip" }
        return parts.joined(separator: " · ")
    }

    /// Marker visibility is independent of whether this audio version can
    /// safely use them for automatic skipping.
    private func visibleAdRegions(for episode: Episode) -> [AdRegion] {
        episode.adMarkers.filter { !$0.isDeleted }.compactMap {
            AdRegion.sanitized(
                startSeconds: $0.startSeconds,
                endSeconds: $0.endSeconds,
                kind: $0.kind,
                episodeDuration: player.duration
            )
        }
    }

    private var transportControls: some View {
        HStack(spacing: 36) {
            Button { player.skipBackward(15) } label: {
                Image(systemName: "gobackward.15")
                    .font(.title)
                    .frame(minWidth: 44, minHeight: 44)
            }
            Button {
                player.togglePlayPause()
            } label: {
                ZStack {
                    Image(systemName: player.isPlaying ? "pause.circle.fill" : "play.circle.fill")
                        .font(.system(size: 64))
                        .opacity(player.isWaitingForAudio ? 0.35 : 1)
                    if player.isWaitingForAudio {
                        ProgressView()
                    }
                }
            }
            Button { player.skipForward(30) } label: {
                Image(systemName: "goforward.30")
                    .font(.title)
                    .frame(minWidth: 44, minHeight: 44)
            }
        }
    }

    private var playbackOptions: some View {
        HStack(spacing: 8) {
            Picker("Speed", selection: Binding(
                get: { player.playbackRate },
                set: { player.setPlaybackRate($0) }
            )) {
                ForEach(PlaybackSpeed.options, id: \.self) { rate in
                    Text(PlaybackSpeed.label(for: rate)).tag(rate)
                }
            }
            .pickerStyle(.menu)
            .frame(minHeight: 44)
            Spacer(minLength: 0)
            if globalAdSkippingEnabled, player.adRegions.contains(where: { $0.kind == .ad }) {
                Toggle("Play ads", isOn: Binding(
                    get: { player.playAdsForCurrentEpisode },
                    set: { player.setPlayAdsForCurrentEpisode($0) }
                ))
                .font(.subheadline)
                .fixedSize()
                .frame(minHeight: 44)
            }
        }
        .padding(.horizontal, 8)
    }

    /// Streaming / preparing / buffering / unavailable state, with Retry
    /// and Download when the episode can't play right now.
    @ViewBuilder
    private func playbackStatus(compact: Bool) -> some View {
        VStack(spacing: 8) {
            if let message = player.statusMessage {
                HStack(spacing: 8) {
                    if player.isWaitingForAudio {
                        ProgressView()
                    } else if player.isUnavailable {
                        Image(systemName: "exclamationmark.triangle.fill")
                            .foregroundStyle(.orange)
                    }
                    Text(message)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.leading)
                        .lineLimit(2)
                }
            }
            if player.isUnavailable {
                HStack(spacing: 12) {
                    Button {
                        player.retry()
                    } label: {
                        Label("Retry", systemImage: "arrow.clockwise")
                    }
                    if player.offersDownload {
                        Button {
                            player.downloadCurrentEpisode()
                        } label: {
                            Label("Download", systemImage: "arrow.down.circle")
                        }
                    }
                }
                .buttonStyle(.bordered)
            } else if player.sourceKind == .stream, !compact {
                Label("Streaming from your server", systemImage: "antenna.radiowaves.left.and.right")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            if player.markersSuppressedForLocalFile {
                Text("This download differs from the server audio, so automatic skipping is off.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
            }
        }
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
