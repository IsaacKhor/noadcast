# Ad marker alignment

Markers use seconds from the audio samples decoded by the server. Their
boundaries are compared with `AVPlayer`'s playback clock on iOS, so both the
timeline and the exact audio render must agree.

## Confirmed failure paths

- A headerless variable-bit-rate MP3 can have a container duration estimated
  from its early bit rate. The server replaces that estimate after a full
  decode, but the player previously overwrote the decoded duration with
  `AVPlayerItem.duration` on every tick and clipped or dropped markers against
  that estimate. Playback now retains the server's measured duration for
  marker sanitization and skip decisions. `AVURLAsset` still requests precise
  timing for seeks; a media file whose player clock itself is inaccurate can
  still land short, where the skip planner's bounded retry protects playback.
- A device download previously recorded the response ETag as its local hash,
  falling back to the **current** server mirror hash when the ETag was absent.
  A server re-download or stale mirror could therefore label different local
  bytes as a match and apply markers to them. The device now hashes the file
  it actually received, verifies a SHA-256 ETag when present, and requires the
  hashes to match before applying markers when the server provides a hash.
- A playing stream can outlive a sync that changes the episode's audio hash.
  Sync now reports an audio identity change even if marker rows are unchanged;
  the player suppresses markers on the old item. For signed URLs that include
  a bound `sha` query parameter, the player also checks that hash when the
  item is first installed, covering a stale local mirror. The server binds
  that hash into the URL signature and returns `409 audioVersionChanged` for
  later GET/HEAD requests if the stored audio changes.

## Remaining timing limits

These are code-supported sources of uncertainty, not a diagnosis of any
specific reported episode; no episode or recording was supplied here.

- `tiny.en` supplies word timestamps, which the sentence joiner groups before
  classification. A word boundary can be imprecise even when the file's
  decoded timebase is correct. The classifier sees sentence intervals and
  makes a semantic choice about what is an ad; it does not inspect every
  acoustic transition.
- The silence map comes from gaps between transcribed words. Music, quiet
  speech, or words the model missed can look like silence. The finalizer can
  move a chosen boundary to a silence midpoint by up to two seconds, so a
  visibly shifted marker may reflect the intentional snap rather than a
  transport or playback offset.
- Damaged MP3 frames can be dropped or concealed differently by the server
  decoder and AVFoundation. A bad frame, VBR seek index, or a player decoder
  difference can make a seek land away from the requested sample position.
  The server's MP3 break cues use cumulative decoded frame durations and are
  conservative classifier hints, with roughly a frame of timing uncertainty
  plus resampler delay; they do not modify playback audio or marker snapping.
  The current player asks for precise timing and retries a short landing at
  most three times; those safeguards cannot guarantee sample-exact seeks on
  every media file.

## Diagnosing a reported episode

Compare the server episode's `audioSha256`, `durationSeconds`,
`durationIsMeasured`, and marker boundaries from `GET /api/v1/episodes/{id}`
with the audio actually playing. For a device download, compare its SHA-256
with `audioSha256`; a matching byte count alone is insufficient. For a stream,
check the signed URL's `sha` parameter and whether sync updated the audio
identity during playback. If hashes match but skips land short, inspect the
MP3 seek behavior separately from marker generation: the player has a
three-attempt guard against repeatedly seeking into the same segment.
