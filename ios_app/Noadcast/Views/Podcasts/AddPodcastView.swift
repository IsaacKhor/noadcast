import SwiftUI
import SwiftData

struct AddPodcastView: View {
    @Environment(\.modelContext) private var context
    @Environment(\.dismiss) private var dismiss

    @State private var mode: Mode = .search
    @State private var urlText: String = ""
    @State private var searchText: String = ""
    @State private var results: [ITunesPodcastResult] = []
    @State private var loading = false
    @State private var error: String?

    enum Mode: String, CaseIterable {
        case search = "Search"
        case url = "RSS URL"
    }

    var body: some View {
        NavigationStack {
            VStack {
                Picker("Mode", selection: $mode) {
                    ForEach(Mode.allCases, id: \.self) { Text($0.rawValue).tag($0) }
                }
                .pickerStyle(.segmented)
                .padding()

                switch mode {
                case .search: searchPanel
                case .url: urlPanel
                }
            }
            .navigationTitle("Add Podcast")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
            }
            .alert("Error", isPresented: .constant(error != nil), actions: {
                Button("OK") { error = nil }
            }, message: {
                Text(error ?? "")
            })
        }
    }

    @ViewBuilder
    private var searchPanel: some View {
        VStack(spacing: 0) {
            HStack {
                TextField("Search podcasts", text: $searchText)
                    .textFieldStyle(.roundedBorder)
                    .autocorrectionDisabled()
                    .textInputAutocapitalization(.never)
                    .submitLabel(.search)
                    .onSubmit { Task { await runSearch() } }
                Button("Search") { Task { await runSearch() } }
                    .disabled(searchText.isEmpty || loading)
            }
            .padding(.horizontal)

            if loading {
                ProgressView().padding()
            }

            List(results) { result in
                Button {
                    Task { await subscribe(result: result) }
                } label: {
                    HStack(spacing: 12) {
                        AsyncImage(url: result.artworkURL) { phase in
                            switch phase {
                            case .success(let image): image.resizable()
                            default: Color.gray.opacity(0.2)
                            }
                        }
                        .frame(width: 48, height: 48)
                        .clipShape(RoundedRectangle(cornerRadius: 6))
                        VStack(alignment: .leading) {
                            Text(result.collectionName).font(.headline)
                            Text(result.artistName).font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Image(systemName: "plus.circle")
                    }
                }
                .buttonStyle(.plain)
            }
        }
    }

    @ViewBuilder
    private var urlPanel: some View {
        Form {
            Section("Feed URL") {
                TextField("https://example.com/feed.xml", text: $urlText)
                    .keyboardType(.URL)
                    .autocorrectionDisabled()
                    .textInputAutocapitalization(.never)
            }
            Section {
                Button {
                    Task { await subscribeFromURL() }
                } label: {
                    if loading { ProgressView() } else { Text("Subscribe") }
                }
                .disabled(URL(string: urlText) == nil || loading)
            }
        }
    }

    private func runSearch() async {
        loading = true
        defer { loading = false }
        do {
            results = try await ITunesSearchService.shared.search(term: searchText)
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func subscribe(result: ITunesPodcastResult) async {
        guard let feedURL = result.feedURL else {
            self.error = "This podcast doesn't expose a feed URL."
            return
        }
        loading = true
        defer { loading = false }
        do {
            _ = try await SubscriptionService.shared.subscribe(feedURL: feedURL, in: context)
            dismiss()
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func subscribeFromURL() async {
        guard let url = URL(string: urlText) else { return }
        loading = true
        defer { loading = false }
        do {
            _ = try await SubscriptionService.shared.subscribe(feedURL: url, in: context)
            dismiss()
        } catch {
            self.error = error.localizedDescription
        }
    }
}
