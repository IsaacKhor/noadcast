import Foundation
import Testing
@testable import Noadcast

struct EpisodeChapterTests {
    private func bigEndian(_ value: Int) -> [UInt8] {
        [24, 16, 8, 0].map { UInt8((value >> $0) & 0xff) }
    }

    private func synchsafe(_ value: Int) -> [UInt8] {
        [21, 14, 7, 0].map { UInt8((value >> $0) & 0x7f) }
    }

    private func frame(_ id: String, body: [UInt8], version: UInt8, flags: UInt8 = 0) -> [UInt8] {
        Array(id.utf8) + (version == 4 ? synchsafe(body.count) : bigEndian(body.count)) + [0, flags] + body
    }

    private func chapter(id: String, start: Int, end: Int, title: String, version: UInt8) -> [UInt8] {
        let titleFrame = frame("TIT2", body: [3] + Array(title.utf8), version: version)
        let body = Array(id.utf8) + [0] + bigEndian(start) + bigEndian(end)
            + bigEndian(0xffff_ffff) + bigEndian(0xffff_ffff) + titleFrame
        return frame("CHAP", body: body, version: version)
    }

    private func tag(version: UInt8) -> Data {
        // Out-of-order frames and a large title exercise frame-size handling
        // in both ID3v2.3 and synchsafe ID3v2.4.
        let frames = chapter(id: "second", start: 130_500, end: 0xffff_ffff,
                             title: String(repeating: "B", count: 135), version: version)
            + chapter(id: "first", start: 22_240, end: 130_500,
                      title: "Introduction", version: version)
        return Data([0x49, 0x44, 0x33, version, 0, 0] + synchsafe(frames.count) + frames)
    }

    @Test(arguments: [UInt8(3), UInt8(4)])
    func parsesMP3Chapters(version: UInt8) throws {
        let chapters = try #require(ID3ChapterParser.parse(tag(version: version)))
        #expect(chapters.count == 2)
        #expect(chapters[0].id == "first")
        #expect(chapters[0].title == "Introduction")
        #expect(chapters[0].startSeconds == 22.24)
        #expect(chapters[0].endSeconds == 130.5)
        #expect(chapters[1].startSeconds == 130.5)
        #expect(chapters[1].endSeconds == nil)
        #expect(chapters[1].title.count == 135)
    }

    @Test func truncatedFramesCannotEscapeTagBounds() {
        var broken = tag(version: 4)
        broken.removeLast(2)
        #expect(ID3ChapterParser.parse(broken) == nil)
        #expect(ID3ChapterParser.parse(Data([0x49, 0x44, 0x33])) == nil)
    }

    @Test func duplicateElementIDsRemainUniqueForSwiftUIRows() throws {
        let frames = chapter(id: "reused", start: 0, end: 5_000, title: "One", version: 4)
            + chapter(id: "reused", start: 5_000, end: 10_000, title: "Two", version: 4)
        let bytes = Data([0x49, 0x44, 0x33, 4, 0, 0] + synchsafe(frames.count) + frames)
        let chapters = try #require(ID3ChapterParser.parse(bytes))
        #expect(chapters.map(\.id) == ["reused", "reused#2"])
    }

    @Test func v24UnsynchronizedTitleFrameIsDecoded() throws {
        let encodedTitle: [UInt8] = [1, 0xff, 0x00, 0xfe, 0x48, 0, 0x69, 0]
        let titleFrame = frame("TIT2", body: encodedTitle, version: 4, flags: 0x02)
        let body = Array("start".utf8) + [0] + bigEndian(0) + bigEndian(10_000)
            + bigEndian(0xffff_ffff) + bigEndian(0xffff_ffff) + titleFrame
        let chaptersFrame = frame("CHAP", body: body, version: 4)
        let bytes = Data([0x49, 0x44, 0x33, 4, 0, 0] + synchsafe(chaptersFrame.count) + chaptersFrame)
        let chapters = try #require(ID3ChapterParser.parse(bytes))
        #expect(chapters.first?.title == "Hi")
    }

    @Test func v24TagAndFrameFlagsDecodeEachBodyOnlyOnce() throws {
        let frames = [(255, 10_000, "One"), (10_000, 20_000, "Two")].flatMap { start, end, title in
            let plain = chapter(id: title, start: start, end: end, title: title, version: 4)
            let escaped = plain.dropFirst(10).flatMap { byte -> [UInt8] in
                byte == 0xff ? [byte, 0] : [byte]
            }
            return frame("CHAP", body: escaped, version: 4, flags: 0x02)
        }
        let bytes = Data([0x49, 0x44, 0x33, 4, 0, 0x80] + synchsafe(frames.count) + frames)
        let chapters = try #require(ID3ChapterParser.parse(bytes))
        #expect(chapters.map(\.title) == ["One", "Two"])
        #expect(chapters.map(\.startSeconds) == [0.255, 10])
        #expect(chapters.map(\.endSeconds) == [10, 20])
    }

    @Test func v23TagLevelUnsynchronizationPreservesChapterTitle() throws {
        let titleFrame = frame("TIT2", body: [0, 0xff, 0x41], version: 3)
        let body = Array("latin".utf8) + [0] + bigEndian(0) + bigEndian(10_000)
            + bigEndian(0xffff_ffff) + bigEndian(0xffff_ffff) + titleFrame
        let original = frame("CHAP", body: body, version: 3)
        var unsynchronized: [UInt8] = []
        for byte in original {
            unsynchronized.append(byte)
            if byte == 0xff { unsynchronized.append(0) }
        }
        let bytes = Data([0x49, 0x44, 0x33, 3, 0, 0x80]
                         + synchsafe(unsynchronized.count) + unsynchronized)
        let chapters = try #require(ID3ChapterParser.parse(bytes))
        #expect(chapters.first?.title == "ÿA")
    }

    @Test func readsLocalMP3WithoutRelyingOnAVFoundationChapterSupport() async throws {
        let file = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString + ".mp3")
        defer { try? FileManager.default.removeItem(at: file) }
        try tag(version: 4).write(to: file)
        let chapters = await EpisodeChapterReader.read(from: file)
        #expect(chapters.first?.title == "Introduction")
        #expect(chapters.map(\.startSeconds) == [22.24, 130.5])
    }
}
