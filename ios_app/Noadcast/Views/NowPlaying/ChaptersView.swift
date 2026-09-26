import SwiftUI

/// The page to the right of the transport controls. Its rows seek to the
/// chapter start while keeping the listener on the chapter page.
struct ChaptersView: View {
    let chapters: [EpisodeChapter]
    let currentTime: Double
    let isLoading: Bool
    let hasAudioItem: Bool
    let onSeek: (Double) -> Void

    var body: some View {
        Group {
            if isLoading {
                ProgressView("Loading chapters…")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if chapters.isEmpty {
                ContentUnavailableView {
                    Label("No Chapters", systemImage: "list.bullet")
                } description: {
                    Text(hasAudioItem
                         ? "This audio file has no embedded chapters."
                         : "Play or download this episode to load its embedded chapters.")
                }
            } else {
                List {
                    Section("Chapters") {
                        ForEach(Array(chapters.enumerated()), id: \.element.id) { index, chapter in
                            let nextStart = chapters.indices.contains(index + 1)
                                ? chapters[index + 1].startSeconds : nil
                            let end = [chapter.endSeconds, nextStart].compactMap { $0 }.min()
                            let timeLabel = end.map {
                                "\(TimeFormatting.timestamp(chapter.startSeconds))–\(TimeFormatting.timestamp($0))"
                            } ?? TimeFormatting.timestamp(chapter.startSeconds)
                            let active = currentTime >= chapter.startSeconds && end.map { currentTime < $0 } != false
                            Button {
                                onSeek(chapter.startSeconds)
                            } label: {
                                HStack(spacing: 12) {
                                    Image(systemName: active ? "waveform" : "play.fill")
                                        .font(.caption)
                                        .frame(width: 22)
                                    VStack(alignment: .leading, spacing: 4) {
                                        Text(chapter.title)
                                            .font(.body)
                                            .foregroundStyle(.primary)
                                            .multilineTextAlignment(.leading)
                                        Text(timeLabel)
                                            .font(.caption.monospacedDigit())
                                            .foregroundStyle(.secondary)
                                    }
                                    Spacer()
                                }
                                .foregroundColor(active ? .accentColor : .secondary)
                                .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                            .accessibilityLabel("\(chapter.title), \(timeLabel)")
                            .accessibilityHint("Seek to chapter")
                        }
                    }
                }
                .listStyle(.plain)
            }
        }
        .overlay(alignment: .bottom) {
            Text("Swipe right for player")
                .font(.caption2)
                .foregroundStyle(.secondary)
                .padding(.bottom, 6)
                .allowsHitTesting(false)
        }
    }
}
