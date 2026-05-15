import SwiftUI

struct ShowNotesView: View {
    let episode: Episode

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    Text(episode.title)
                        .font(.title2.bold())
                    if let date = episode.publishedAt {
                        Text(date.formatted(date: .abbreviated, time: .omitted))
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                    }
                    Divider()
                    Text(rendered(episode.episodeDescription ?? "No show notes."))
                }
                .padding()
            }
            .navigationTitle("Show Notes")
            .navigationBarTitleDisplayMode(.inline)
        }
    }

    private func rendered(_ raw: String) -> AttributedString {
        // Best-effort HTML rendering; falls back to the raw string.
        if let data = raw.data(using: .utf8),
           let ns = try? NSAttributedString(
               data: data,
               options: [
                   .documentType: NSAttributedString.DocumentType.html,
                   .characterEncoding: String.Encoding.utf8.rawValue
               ],
               documentAttributes: nil
           ) {
            return AttributedString(ns)
        }
        return AttributedString(raw)
    }
}
