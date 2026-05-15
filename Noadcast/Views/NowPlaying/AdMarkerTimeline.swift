import SwiftUI

/// A scrubber that overlays ad-region markers on the playback timeline.
struct AdMarkerTimeline: View {
    let currentTime: Double
    let duration: Double
    let adRegions: [AdRegion]
    let onSeek: (Double) -> Void

    @State private var dragValue: Double?

    private var displayedTime: Double { dragValue ?? currentTime }

    private static let trackHeight: CGFloat = 6
    private static let adHeight: CGFloat = 18
    private static let thumbDiameter: CGFloat = 16

    var body: some View {
        VStack(spacing: 6) {
            GeometryReader { geo in
                let width = geo.size.width
                ZStack {
                    // Track background.
                    Capsule()
                        .fill(.quaternary)
                        .frame(width: width, height: Self.trackHeight)

                    if duration > 0 {
                        // Ad regions: taller than the track so they bracket
                        // the playhead. Bright fill + faint top/bottom border
                        // so they read clearly against both the unfilled and
                        // filled portions of the bar.
                        ForEach(Array(adRegions.enumerated()), id: \.offset) { _, region in
                            let start = max(0, min(1, region.startSeconds / duration))
                            let end = max(0, min(1, region.endSeconds / duration))
                            let xStart = start * width
                            let xEnd = end * width
                            let regionWidth = max(3, xEnd - xStart)
                            RoundedRectangle(cornerRadius: 3)
                                .fill(Color.orange)
                                .overlay(
                                    RoundedRectangle(cornerRadius: 3)
                                        .stroke(Color.orange.opacity(0.4), lineWidth: 0.5)
                                )
                                .frame(width: regionWidth, height: Self.adHeight)
                                .position(
                                    x: xStart + regionWidth / 2,
                                    y: geo.size.height / 2
                                )
                        }

                        // Progress fill on top of track, behind the ad
                        // markers' top half so the markers stay visible.
                        let progressWidth = max(0, displayedTime / duration) * width
                        Capsule()
                            .fill(Color.accentColor)
                            .frame(width: progressWidth, height: Self.trackHeight)
                            .position(x: progressWidth / 2, y: geo.size.height / 2)

                        // Thumb.
                        Circle()
                            .fill(Color.accentColor)
                            .frame(width: Self.thumbDiameter, height: Self.thumbDiameter)
                            .position(
                                x: max(Self.thumbDiameter / 2,
                                       min(width - Self.thumbDiameter / 2,
                                           (displayedTime / duration) * width)),
                                y: geo.size.height / 2
                            )
                    }
                }
                .contentShape(Rectangle())
                .gesture(
                    DragGesture(minimumDistance: 0)
                        .onChanged { value in
                            guard duration > 0, width > 0 else { return }
                            let pct = max(0, min(1, value.location.x / width))
                            dragValue = pct * duration
                        }
                        .onEnded { _ in
                            if let v = dragValue { onSeek(v) }
                            dragValue = nil
                        }
                )
            }
            .frame(height: Self.adHeight)

            HStack {
                Text(TimeFormatting.timestamp(displayedTime))
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
                Spacer()
                Text("-" + TimeFormatting.timestamp(max(0, duration - displayedTime)))
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
        }
    }
}
