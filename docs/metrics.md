# Metric definitions and data retention

All timestamps are stored in UTC. The **display timezone** (Account & security → Access) decides how times are shown
and where day boundaries fall for “Today”, “7 days”, “30 days”, custom ranges, and the daily chart buckets.

## Where each number comes from

| Metric | Definition | Source | Not |
|---|---|---|---|
| **Segment skips** | Count of `Skipping segment: seeking to …` lines | Observed (upstream log) | Not a confirmed seek: upstream never logs whether the TV obeyed. Shown as “requested”. |
| **Skipped segment length** | Sum over attributed skips of (merged segment end − start) | Inferred: the logged seek target is matched to the SponsorBlock segment list for that video, merged exactly like upstream does (`process_segments`) | Not measured time saved: a skip that lands mid-segment, a seek the TV ignored, or a user who seeks back are not visible. |
| Attribution | `exact` (seek target equals segment end ±0.01 s) or `nearest` (±0.75 s); otherwise `none` | Inferred | Skips with `none`/`pending` count as skips but add no duration, and appear as “Category unknown” / “Not matched yet”. |
| **Category breakdown** | Each skip counted once per category of its merged segment; duration split evenly between categories of a merged segment | Inferred | Overlapping segments are merged first, so time is never double-counted. |
| **YouTube ads skipped** | Count of `Ad can be skipped, skipping` | Observed | Not confirmed. |
| **YouTube ads muted** | Count of `Ad has started, muting` | Observed | |
| Ad mute time | Time from a mute to the next `Ad has ended, unmuting` on the same TV within 10 minutes | Observed pair | **Not time saved.** Repeated unmute lines (upstream logs them on every play event) are ignored. Unpaired mutes have no duration. |
| **Videos played / playback sessions** | Distinct video IDs / runs of `Playing video X` on a TV (a new session starts when the video changes or after 6 h) | Observed | |
| Device states | See the table in [upstream-capabilities.md](upstream-capabilities.md); log silence never means “offline”. | Observed edges | |

## Zero, unknown, unavailable, not collected

* **Not collected yet** — collection has never received an upstream record (tiles say so instead of “0”).
* **Unavailable** — no log source is configured.
* **0** — collection covered the range and nothing happened.
* Ranges that start before collection began show “Collection started …; earlier dates have no data, not zero activity”.
* Collection gaps (source unreachable, container stopped, lines Docker discarded before they were read) are recorded
  with start/end and shown on the Overview and in Diagnostics. Statistics are never reset or zeroed during an outage;
  the last data stays visible with its timestamp.

## Duplicates and repeats

Every upstream line is identified by *source container + Docker's nanosecond timestamp + content hash*. Re-reading
the same lines (restart of the web UI, Docker returning older lines after an upstream restart) adds nothing.
Watching the same video again produces new lines with new timestamps and is counted again, as it should be.
The read position (checkpoint) is committed together with the records; at most the last second of lines is
re-read after a crash, and those dedupe.

## Retention

| Data | Default | Setting |
|---|---|---|
| Detailed events (skips, ads, connections, errors) | 365 days | `EVENT_RETENTION_DAYS` (min 7) |
| Daily totals (count + duration per day, device, kind, category) | kept indefinitely | — |
| Raw iSponsorBlockTV log lines (redacted) | 14 days or 200 000 rows, whichever is smaller | `UPSTREAM_LOG_RETENTION_DAYS`, `UPSTREAM_LOG_MAX_ROWS` |
| Video metadata cache | kept | — |

Events older than the retention window are first rolled up into daily totals, then deleted, in one transaction.
Daily totals keep counts and durations per UTC day; they do not keep per-video or per-channel detail, so the
Channels page and activity search only cover the detailed window. Removing a TV from the configuration keeps its
history; **Delete history** on the Devices page removes it explicitly.

## Metadata enrichment

Optional (`ENRICHMENT_ENABLED`, default on) and independent of collection: an event is stored first, enriched later.

* SponsorBlock segment lookup (same privacy-preserving hashed request upstream uses), at most 2 requests/s, paused 60 s
  on HTTP 429, retried with backoff (1 min → 24 h) on failure.
* Title and channel via YouTube oEmbed (no API key), at most 1 request/s; a YouTube Data API key (optional) adds
  stable channel IDs. Private/removed videos are marked “unavailable”, never guessed.
* Channel links appear only when the channel is known. A channel listed on the Channels page had a segment skipped in
  one of its videos; that says nothing about the channel being blocked or whitelisted.
