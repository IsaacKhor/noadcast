import AVFoundation
import Foundation

nonisolated struct EpisodeChapter: Identifiable, Equatable, Sendable {
    let id: String
    let title: String
    let startSeconds: Double
    let endSeconds: Double?
}

/// Reads embedded chapters from the audio actually being played. ID3 CHAP is
/// parsed explicitly because AVFoundation does not consistently expose MP3
/// chapter frames as AVTimedMetadataGroups.
nonisolated enum EpisodeChapterReader {
    static let maximumTagBytes = 4 * 1024 * 1024

    static func read(from url: URL) async -> [EpisodeChapter] {
        guard !Task.isCancelled else { return [] }
        // Signed stream URLs often have no .mp3 suffix, so sniff the header
        // regardless of the path extension.
        if let data = try? await readID3Prefix(from: url),
           let chapters = ID3ChapterParser.parse(data), !chapters.isEmpty {
            return chapters
        }
        guard !Task.isCancelled else { return [] }
        // Also covers MP4/M4A chapter tracks, and any MP3 chapters which
        // AVFoundation happens to expose on this device.
        let asset = AVURLAsset(url: url)
        let groups = (try? await asset.loadChapterMetadataGroups(
            bestMatchingPreferredLanguages: Locale.preferredLanguages
        )) ?? []
        return groups.enumerated().compactMap { index, group -> EpisodeChapter? in
            let seconds = group.timeRange.start.seconds
            guard seconds.isFinite, seconds >= 0 else { return nil }
            let title = group.items.first(where: { $0.commonKey == .commonKeyTitle })?.stringValue
                ?? group.items.compactMap(\.stringValue).first
                ?? "Chapter \(index + 1)"
            let duration = group.timeRange.duration.seconds
            return EpisodeChapter(
                id: "native-\(index)-\(seconds)", title: title,
                startSeconds: seconds,
                endSeconds: duration.isFinite && duration > 0 ? seconds + duration : nil
            )
        }.sorted { $0.startSeconds < $1.startSeconds }
    }

    private static func readID3Prefix(from url: URL) async throws -> Data? {
        let header = try await readPrefix(from: url, count: 10)
        guard header.count == 10, header.starts(with: [0x49, 0x44, 0x33]),
              let size = ID3ChapterParser.synchsafe(header, at: 6) else { return nil }
        let total = size + 10
        guard total <= maximumTagBytes else { return nil }
        return try await readPrefix(from: url, count: total)
    }

    private static func readPrefix(from url: URL, count: Int) async throws -> Data {
        try Task.checkCancellation()
        if url.isFileURL {
            let handle = try FileHandle(forReadingFrom: url)
            defer { try? handle.close() }
            return try handle.read(upToCount: count) ?? Data()
        }
        var request = URLRequest(url: url)
        request.setValue("bytes=0-\(count - 1)", forHTTPHeaderField: "Range")
        request.timeoutInterval = 15
        // Use a session scoped to this prefix. Cancelling it after the loop
        // also stops a server which ignored Range and started sending the
        // entire episode.
        let session = URLSession(configuration: .ephemeral)
        defer { session.invalidateAndCancel() }
        let (bytes, response) = try await session.bytes(for: request)
        guard let response = response as? HTTPURLResponse,
              response.statusCode == 206 || response.statusCode == 200 else { return Data() }
        var data = Data()
        data.reserveCapacity(count)
        for try await byte in bytes {
            data.append(byte)
            if data.count == count { break }
        }
        return data
    }
}

/// ID3v2.3/v2.4 CHAP frames: element ID, start/end milliseconds, byte
/// offsets, then nested TIT2 frame. All lengths are checked before slicing.
nonisolated enum ID3ChapterParser {
    static func synchsafe(_ data: Data, at offset: Int) -> Int? {
        guard offset >= 0, offset + 4 <= data.count else { return nil }
        let bytes = data[offset..<(offset + 4)]
        guard bytes.allSatisfy({ $0 < 128 }) else { return nil }
        return bytes.reduce(0) { ($0 << 7) | Int($1) }
    }

    static func parse(_ data: Data) -> [EpisodeChapter]? {
        guard data.count >= 10, data.starts(with: [0x49, 0x44, 0x33]),
              [3, 4].contains(data[3]),
              let size = synchsafe(data, at: 6), size + 10 <= data.count else { return nil }
        let version = data[3]
        let payload = Data(data[10..<(size + 10)])
        let extended = data[5] & 0x40 != 0
        let chapters: [EpisodeChapter]
        if version == 3, data[5] & 0x80 != 0 {
            // v2.3 sizes describe the original frames, before tag-wide escaping.
            chapters = parseFrames(deunsynchronize(payload), version: version, extended: extended)
        } else {
            // v2.4 sizes include escaped bytes. Locate each frame first, then
            // decode its body once, even when both tag and frame flags are set.
            chapters = parseFrames(payload, version: version, extended: extended,
                                   allFramesUnsynchronized: data[5] & 0x80 != 0)
        }
        var seenStarts = Set<Double>()
        var seenIDs = Set<String>()
        return chapters.sorted { $0.startSeconds < $1.startSeconds }
            .filter { seenStarts.insert($0.startSeconds).inserted }
            .map { chapter in
                var id = chapter.id
                var suffix = 2
                while !seenIDs.insert(id).inserted {
                    id = "\(chapter.id)#\(suffix)"
                    suffix += 1
                }
                return EpisodeChapter(id: id, title: chapter.title,
                                      startSeconds: chapter.startSeconds, endSeconds: chapter.endSeconds)
            }
    }

    private static func parseFrames(
        _ data: Data, version: UInt8, extended: Bool, allFramesUnsynchronized: Bool = false
    ) -> [EpisodeChapter] {
        var offset = 0
        if extended {
            guard data.count >= 4,
                  let extraSize = version == 4 ? synchsafe(data, at: 0) : integer(data, at: 0) else { return [] }
            offset = version == 4 ? extraSize : 4 + extraSize
        }
        guard offset <= data.count else { return [] }
        var chapters: [EpisodeChapter] = []
        while offset + 10 <= data.count {
            guard let header = frame(data, at: offset, limit: data.count, version: version) else { break }
            if header.id == "CHAP",
               let body = preparedBody(data, frame: header, version: version,
                                       forceUnsynchronization: allFramesUnsynchronized),
               let chapter = chapter(body, version: version) {
                chapters.append(chapter)
            }
            offset = header.end
        }
        return chapters
    }

    private static func frame(_ data: Data, at offset: Int, limit: Int, version: UInt8)
        -> (id: String, body: Int, end: Int, flags: UInt8)? {
        guard offset + 10 <= limit,
              let id = String(data: data[offset..<(offset + 4)], encoding: .ascii),
              id.utf8.allSatisfy({ ($0 >= 65 && $0 <= 90) || ($0 >= 48 && $0 <= 57) }),
              let size = version == 4 ? synchsafe(data, at: offset + 4) : integer(data, at: offset + 4),
              size > 0, size <= limit - offset - 10 else { return nil }
        return (id, offset + 10, offset + 10 + size, data[offset + 9])
    }

    private static func preparedBody(_ data: Data,
        frame: (id: String, body: Int, end: Int, flags: UInt8), version: UInt8,
        forceUnsynchronization: Bool = false) -> Data? {
        let flags = frame.flags
        if version == 4, flags & 0x0c != 0 { return nil } // compressed/encrypted
        if version == 3, flags & 0xc0 != 0 { return nil }
        var body = Data(data[frame.body..<frame.end])
        if version == 4 {
            if forceUnsynchronization || flags & 0x02 != 0 { body = deunsynchronize(body) }
            if flags & 0x40 != 0 { guard !body.isEmpty else { return nil }; body = Data(body.dropFirst()) }
            if flags & 0x01 != 0 { guard body.count >= 4 else { return nil }; body = Data(body.dropFirst(4)) }
        } else if flags & 0x20 != 0 {
            guard !body.isEmpty else { return nil }
            body = Data(body.dropFirst())
        }
        return body
    }

    private static func chapter(_ data: Data, version: UInt8) -> EpisodeChapter? {
        guard let terminator = data.firstIndex(of: 0),
              terminator + 17 <= data.count,
              let start = integer(data, at: terminator + 1),
              let finish = integer(data, at: terminator + 5) else { return nil }
        let elementID = String(decoding: data[0..<terminator], as: UTF8.self)
        var nested = terminator + 17
        var title: String?
        while nested + 10 <= data.count {
            guard let frame = frame(data, at: nested, limit: data.count, version: version) else { break }
            if frame.id == "TIT2", let body = preparedBody(data, frame: frame, version: version) {
                title = decodeTitle(body)
            }
            nested = frame.end
        }
        let cleanTitle = title?.trimmingCharacters(in: .whitespacesAndNewlines)
        return EpisodeChapter(
            id: elementID.isEmpty ? "chap-\(start)" : elementID,
            title: cleanTitle.flatMap { $0.isEmpty ? nil : $0 } ?? "Chapter \(start / 1000)",
            startSeconds: Double(start) / 1000,
            endSeconds: finish == Int(UInt32.max) || finish <= start ? nil : Double(finish) / 1000
        )
    }

    private static func integer(_ data: Data, at offset: Int) -> Int? {
        guard offset >= 0, offset + 4 <= data.count else { return nil }
        return data[offset..<(offset + 4)].reduce(0) { ($0 << 8) | Int($1) }
    }

    private static func decodeTitle(_ data: Data.SubSequence) -> String? {
        guard let encoding = data.first else { return nil }
        let body = Data(data.dropFirst())
        switch encoding {
        case 0: return String(data: body, encoding: .isoLatin1)?.trimmingCharacters(in: .controlCharacters)
        case 1: return String(data: body, encoding: .utf16)?.trimmingCharacters(in: .controlCharacters)
        case 2: return String(data: body, encoding: .utf16BigEndian)?.trimmingCharacters(in: .controlCharacters)
        case 3: return String(data: body, encoding: .utf8)?.trimmingCharacters(in: .controlCharacters)
        default: return nil
        }
    }

    private static func deunsynchronize(_ data: Data) -> Data {
        var cleaned = Data()
        cleaned.reserveCapacity(data.count)
        var previousWasFF = false
        for byte in data {
            if previousWasFF && byte == 0 {
                previousWasFF = false
                continue
            }
            cleaned.append(byte)
            previousWasFF = byte == 0xff
        }
        return cleaned
    }
}
