import Foundation

/// On-disk layout for downloaded episode audio and download resume data.
///
/// Nonisolated on purpose: used synchronously from the background download
/// delegate (the temp file must be moved before the callback returns), from
/// the sync engine actor, and from the main actor.
///
/// Files downloaded by this build are named `srv-<serverID>-<random>.<ext>`.
/// The legacy rule (`DownloadService.suggestedFilename`) only ever produced
/// letters, digits and `_`, so the `srv-` prefix can never collide with a
/// legacy file awaiting adoption.
nonisolated enum AudioStorage {
    static let serverFilePrefix = "srv-"

    /// `Application Support/episodes/` — the same directory the previous
    /// build used, so legacy files can be adopted in place.
    static let episodesDirectory: URL = makeDirectory(named: "episodes")

    /// `Application Support/download-resume/` — one blob per episode.
    static let resumeDataDirectory: URL = makeDirectory(named: "download-resume")

    static var applicationSupportDirectory: URL {
        (try? FileManager.default.url(
            for: .applicationSupportDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: true
        )) ?? FileManager.default.temporaryDirectory
    }

    private static func makeDirectory(named name: String) -> URL {
        let dir = applicationSupportDirectory.appendingPathComponent(name, isDirectory: true)
        if !FileManager.default.fileExists(atPath: dir.path) {
            try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        }
        return dir
    }

    static func fileURL(for filename: String) -> URL {
        episodesDirectory.appendingPathComponent(filename)
    }

    static func fileExists(named filename: String?) -> Bool {
        guard let filename else { return false }
        return FileManager.default.fileExists(atPath: fileURL(for: filename).path)
    }

    static func fileSize(named filename: String) -> Int64? {
        let attributes = try? FileManager.default.attributesOfItem(atPath: fileURL(for: filename).path)
        return (attributes?[.size] as? NSNumber)?.int64Value
    }

    static func deleteFile(named filename: String?) {
        guard let filename, !filename.isEmpty else { return }
        try? FileManager.default.removeItem(at: fileURL(for: filename))
    }

    /// Filenames currently in `episodesDirectory`.
    static func listEpisodeFiles() -> [String] {
        (try? FileManager.default.contentsOfDirectory(atPath: episodesDirectory.path)) ?? []
    }

    static func isServerFilename(_ filename: String) -> Bool {
        filename.hasPrefix(serverFilePrefix)
    }

    /// Unique per download, so a stale reference can never point at a
    /// different episode's bytes (ids are reissued if the server database is
    /// rebuilt).
    static func makeFilename(serverID: Int, fileExtension: String) -> String {
        let token = UUID().uuidString.prefix(8).lowercased()
        return "\(serverFilePrefix)\(serverID)-\(token).\(fileExtension)"
    }

    /// File extension for a MIME type (same mapping the previous build used).
    static func fileExtension(forMimeType mimeType: String?) -> String {
        switch mimeType?.lowercased() {
        case let m? where m.contains("mpeg"): return "mp3"
        case let m? where m.contains("mp4"), let m? where m.contains("m4a"): return "m4a"
        case let m? where m.contains("aac"): return "aac"
        case let m? where m.contains("ogg"): return "ogg"
        case let m? where m.contains("wav"): return "wav"
        default: return "mp3"
        }
    }

    /// The previous build's `DownloadService.suggestedFilename(for:mimeType:)`,
    /// verbatim. Used to find legacy files when no export could be written.
    static func legacyFilename(guid: String, mimeType: String?) -> String {
        let ext = fileExtension(forMimeType: mimeType)
        let slug = guid.compactMap { ch -> Character? in
            (ch.isLetter || ch.isNumber) ? ch : "_"
        }
        return "\(String(slug.prefix(80))).\(ext)"
    }

    // MARK: - Resume data

    static func resumeDataURL(serverID: Int) -> URL {
        resumeDataDirectory.appendingPathComponent("\(serverID).resume")
    }

    static func saveResumeData(_ data: Data, serverID: Int) {
        try? data.write(to: resumeDataURL(serverID: serverID), options: .atomic)
    }

    static func loadResumeData(serverID: Int) -> Data? {
        try? Data(contentsOf: resumeDataURL(serverID: serverID))
    }

    static func deleteResumeData(serverID: Int) {
        try? FileManager.default.removeItem(at: resumeDataURL(serverID: serverID))
    }

    /// Resume blobs are keyed by server id; a server rebuild makes them all
    /// meaningless.
    static func deleteAllResumeData() {
        let names = (try? FileManager.default.contentsOfDirectory(atPath: resumeDataDirectory.path)) ?? []
        for name in names {
            try? FileManager.default.removeItem(at: resumeDataDirectory.appendingPathComponent(name))
        }
    }
}
