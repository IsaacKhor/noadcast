# Player verification in Xcode

The Linux workspace cannot build SwiftUI/AVFoundation or run an iOS simulator.
The following checks need Xcode with the iOS 26 SDK; they are not marked passed
by server tests or source inspection.

On 2026-09-26, the final Linux server run completed 623 tests successfully
with one optional test skipped (`unittest discover -s tests -t .`). The iOS
build, Swift tests, queue gestures, and rendered layouts remain unverified.

## Automated checks

Select an available simulator from `xcrun simctl list devices available`, then:

```sh
xcodebuild -project ios_app/Noadcast.xcodeproj -scheme Noadcast \
  -destination 'platform=iOS Simulator,id=<SIMULATOR-UDID>' test
```

`QueueScrollingTests` launches an isolated, in-memory queue with 40 episodes.
It checks both a full swipe and the revealed **Top** button: the moved episode
leaves the viewport, a neighboring episode stays visible, the list does not
return to its beginning, and the moved episode is first when deliberately
scrolling back. The fixture is Debug-only and does not open the user's store.

`PlaybackTests` covers overlapping and adjoining ads, per-kind skip settings,
and the retry guard for streaming seeks that land short. Chapter tests cover
metadata parsing and chapter bounds. Run all app tests to check integration
with SwiftData and the networking code as well.

## Simulator/device checks

1. Open a playing episode with a long title on the smallest supported phone.
   Check portrait, landscape, and larger Dynamic Type sizes. The artwork should
   be smaller and the timeline, transport, speed, ad controls, show notes, and
   output route button should remain accessible without vertically scrolling
   the player controls page. Repeat with buffering and error/retry status visible.
2. Swipe left to the page on the right of the player. Use an MP3 with embedded
   ID3 chapter frames, both streamed and downloaded. Check chapter titles and
   times, tap a chapter, and verify playback seeks and the active chapter changes.
   Switch episodes while chapters load; chapters from the previous episode must
   not appear. Also check an episode without chapters and offline local playback.
3. Open detected skip segments. Each row should show its kind, start/end time,
   and readable content summary. Disable ad skipping and verify the metadata
   remains visible. A local copy that differs from server audio must not apply
   the server's skip times.
4. In a real populated queue, scroll several screens down and move an episode
   to the top using both gestures covered by the UI tests. Check that no swiped
   row remains floating over the list. Repeat with an episode already playing;
   the moved episode should become first under **Up Next**.

Record the Xcode build/test result, device sizes, and any failing step before
claiming the player UI changes are verified.
