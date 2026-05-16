import SwiftUI
import SwiftData

struct NowPlayingView: View {
    @Environment(\.modelContext) private var context

    private var player = PlayerService.shared

    @State private var showTranscript = false
    @State private var showNotes = false
    @State private var showAds = false

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
        .sheet(isPresented: $showTranscript) {
            if let ep = currentEpisode {
                TranscriptView(
                    segments: ep.transcript,
                    adRegions: player.adRegions,
                    onSeek: { time in
                        player.seek(to: time)
                        showTranscript = false
                    }
                )
            }
        }
        .sheet(isPresented: $showNotes) {
            if let ep = currentEpisode {
                ShowNotesView(episode: ep)
            }
        }
        .sheet(isPresented: $showAds) {
            if let ep = currentEpisode {
                AdsTranscriptView(
                    segments: ep.transcript,
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
                    Text(episode.podcast?.title ?? "")
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

                HStack(spacing: 24) {
                    Button {
                        showNotes = true
                    } label: {
                        Label("Show Notes", systemImage: "doc.text")
                    }
                    Button {
                        showTranscript = true
                    } label: {
                        Label("Transcript", systemImage: "text.alignleft")
                    }
                }
                .buttonStyle(.bordered)
            }
            .padding()
        }
    }

    private func artwork(for episode: Episode) -> some View {
        let url = episode.podcast?.artworkDisplayURL
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
                    Text("\(player.adRegions.count) ad\(player.adRegions.count == 1 ? "" : "s") detected")
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
        .padding(.horizontal)
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
