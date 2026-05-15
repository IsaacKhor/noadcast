import SwiftUI

/// Compact persistent media bar shown above the tab bar (à la Apple Music).
/// Tap to expand into the full Now Playing sheet. Mounted in the iOS 26
/// `tabViewBottomAccessory` slot.
///
/// When no episode is loaded the bar still renders (the accessory slot
/// reserves space anyway) but switches to a muted placeholder so the user
/// doesn't see an empty white strip.
struct MiniPlayerBar: View {
    let onTap: () -> Void
    private let player = PlayerService.shared

    private var hasEpisode: Bool { player.currentEpisodeID != nil }

    private var progressFraction: Double {
        guard player.duration > 0 else { return 0 }
        return max(0, min(1, player.currentTime / player.duration))
    }

    var body: some View {
        VStack(spacing: 0) {
            if hasEpisode {
                progressLine
            }
            content
        }
        .background(.bar)
    }

    @ViewBuilder
    private var content: some View {
        if hasEpisode {
            loadedContent
        } else {
            emptyContent
        }
    }

    private var loadedContent: some View {
        Button(action: onTap) {
            HStack(spacing: 12) {
                artwork
                VStack(alignment: .leading, spacing: 1) {
                    Text(player.currentEpisodeTitle)
                        .font(.subheadline.bold())
                        .lineLimit(1)
                    Text(player.currentPodcastTitle)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                Spacer()
                Button {
                    player.togglePlayPause()
                } label: {
                    Image(systemName: player.isPlaying ? "pause.fill" : "play.fill")
                        .font(.title2)
                        .frame(width: 36, height: 36)
                }
                .buttonStyle(.plain)
                Button {
                    player.skipForward(30)
                } label: {
                    Image(systemName: "goforward.30")
                        .font(.title2)
                        .frame(width: 36, height: 36)
                }
                .buttonStyle(.plain)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private var emptyContent: some View {
        HStack(spacing: 12) {
            RoundedRectangle(cornerRadius: 6)
                .fill(.quaternary)
                .frame(width: 40, height: 40)
                .overlay(
                    Image(systemName: "music.note")
                        .foregroundStyle(.tertiary)
                )
            Text("No current episode")
                .font(.subheadline)
                .foregroundStyle(.secondary)
            Spacer()
            Image(systemName: "play.fill")
                .font(.title2)
                .foregroundStyle(.tertiary)
                .frame(width: 36, height: 36)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("No current episode")
    }

    private var artwork: some View {
        AsyncImage(url: player.artworkURL) { phase in
            switch phase {
            case .success(let image): image.resizable()
            default: Color.gray.opacity(0.2)
            }
        }
        .frame(width: 40, height: 40)
        .clipShape(RoundedRectangle(cornerRadius: 6))
    }

    /// Thin progress line above the bar. Includes ad regions so the user can
    /// see them even when collapsed.
    private var progressLine: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Rectangle()
                    .fill(.quaternary)
                    .frame(height: 2)
                ForEach(Array(player.adRegions.enumerated()), id: \.offset) { _, region in
                    let start = region.startSeconds / max(player.duration, 1)
                    let end = region.endSeconds / max(player.duration, 1)
                    let xStart = max(0, min(1, start)) * geo.size.width
                    let xEnd = max(0, min(1, end)) * geo.size.width
                    Rectangle()
                        .fill(Color.orange)
                        .frame(width: max(1.5, xEnd - xStart), height: 2)
                        .offset(x: xStart)
                }
                Rectangle()
                    .fill(Color.accentColor)
                    .frame(width: progressFraction * geo.size.width, height: 2)
            }
        }
        .frame(height: 2)
    }
}
