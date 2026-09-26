import SwiftUI

/// Sheet showing each detected skippable segment (ad, intro, or outro).
/// Reached by tapping the segments summary in `NowPlayingView`.
struct SkipSegmentsView: View {
    let ads: [AdMarker]
    let onSeek: (Double) -> Void

    private var sortedAds: [AdMarker] {
        ads.filter { !$0.isDeleted }.sorted { $0.startSeconds < $1.startSeconds }
    }

    var body: some View {
        NavigationStack {
            Group {
                if sortedAds.isEmpty {
                    ContentUnavailableView {
                        Label("Nothing to skip", systemImage: "speaker.slash")
                    } description: {
                        Text("No intro, outro, or ads detected — or the episode hasn't finished processing yet.")
                    }
                } else {
                    List {
                        ForEach(sortedAds) { ad in
                            SegmentRow(ad: ad, onSeek: onSeek)
                        }
                    }
                }
            }
            .navigationTitle("Skip Segments")
            .navigationBarTitleDisplayMode(.inline)
        }
    }
}

private struct SegmentRow: View {
    let ad: AdMarker
    let onSeek: (Double) -> Void

    private var fallbackTitle: String {
        switch ad.kind {
        case .ad: "Advertisement"
        case .intro: "Intro"
        case .outro: "Outro"
        }
    }

    var body: some View {
        Button {
            onSeek(ad.startSeconds)
        } label: {
            VStack(alignment: .leading, spacing: 7) {
                HStack(spacing: 8) {
                    Text(ad.kind.label.uppercased())
                        .font(.caption2.bold())
                        .foregroundStyle(.white)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 3)
                        .background(ad.kind.tint, in: Capsule())
                    Text("\(TimeFormatting.timestamp(ad.startSeconds))–\(TimeFormatting.timestamp(ad.endSeconds))")
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                    Spacer()
                    Image(systemName: "play.fill")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Text(ad.summary.isEmpty ? fallbackTitle : ad.summary)
                    .font(.subheadline)
                    .foregroundStyle(.primary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(.vertical, 4)
        }
        .buttonStyle(.plain)
        .accessibilityHint("Seek to segment")
    }
}
