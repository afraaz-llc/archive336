import type { CodecPreference, VideoMaxResolution } from "./types"

// Rough bitrate per YouTube stream tier (H.264 / AAC baseline, Mbps).
const BASE_BITRATE_MBPS: Record<VideoMaxResolution, number> = {
  "audio-only": 0.15,
  "360p": 1.0,
  "480p": 1.5,
  "720p": 4.0,
  "1080p": 7.0,
  "1440p": 14.0,
  "2160p": 25.0,
  source: 16.0,
}

// 1 Mbps sustained for 1 hour ≈ 0.45 GB (decimal).
export function estimateGbPerHour(
  resolution: VideoMaxResolution,
  codec: CodecPreference
): number {
  const codecFactor = codec === "efficient" ? 0.6 : 1
  const mbps = BASE_BITRATE_MBPS[resolution] * codecFactor
  return mbps * 0.45
}

/** GB-per-hour to use when projecting a channel's full size, anchored to
 * the user's OWN archived footage when we have any.
 *
 * The generic table above assumes a fixed bitrate (720p ≈ 1.8 GB/hr) that
 * routinely overshoots real files by 2-3x - phone footage, screen
 * recordings and talking-head video all compress far below YouTube's
 * ceiling. Projecting that inflated rate across a whole channel produced a
 * "channel size" several times the actual bill, which read as a
 * contradiction next to the real archived number.
 *
 * So when the channel already has archived bytes, that measured rate is
 * the ground truth for THIS content, and we scale it by the table ratio
 * only to reflect a DIFFERENT resolution/codec than what is archived. With
 * nothing archived yet there is nothing to measure, so we fall back to the
 * generic table.
 */
export function projectedGbPerHour(
  target: { resolution: VideoMaxResolution; codec: CodecPreference },
  measured?: {
    bytes: number
    durationSec: number
    resolution: VideoMaxResolution
    codec: CodecPreference
  }
): number {
  const generic = estimateGbPerHour(target.resolution, target.codec)
  if (!measured || measured.bytes <= 0 || measured.durationSec <= 0) {
    return generic
  }
  const measuredGbPerHour =
    measured.bytes / 1_000_000_000 / (measured.durationSec / 3600)
  const baselineForMeasured = estimateGbPerHour(
    measured.resolution,
    measured.codec
  )
  if (baselineForMeasured <= 0) return measuredGbPerHour
  // Real rate for the archived quality, rescaled to the target quality by
  // the table's own ratio - so changing the resolution picker still moves
  // the estimate, but off a base that matches reality instead of a guess.
  return measuredGbPerHour * (generic / baselineForMeasured)
}

/** Cost of downloading `gb` worth of archive at `pricePerGb` (USD/GB).
 *
 * `pricePerGb` should come from the PricingProvider via `usePrices()`
 * so the number always matches what backend/app/billing.py is charging.
 * Callers pass it in explicitly rather than relying on a hardcoded
 * constant - the constant pattern was a drift hazard.
 */
export function estimateDownloadCostUsd(
  gb: number,
  pricePerGb: number,
): number {
  return gb * pricePerGb
}

/** Rough monthly storage cost in USD for `bytes` of archive at the
 * given price (USD per GB-month). Decimal GB (1_000_000_000 bytes) so
 * the number lines up with how cloud storage is metered + billed. */
export function estimateMonthlyStorageCostUsd(
  bytes: number,
  pricePerGbMonth: number,
): number {
  return (bytes / 1_000_000_000) * pricePerGbMonth
}


/** Returns true when the video's archived quality settings don't match
 * the channel's CURRENT settings - meaning a bulk re-sync would replace
 * this video to bring it in line.
 *
 * "Outdated" includes both upgrades (current resolution > archived) AND
 * downgrades (current < archived) AND codec preference changes. The
 * mental model is: the channel settings are the source of truth, every
 * archived video should mirror them. Consistency beats per-video
 * preservation - no "this one stays at 1080p forever" exceptions.
 *
 * Returns false when archived fields are null (legacy archive that
 * predates the stamping feature). Those count as "unknown" and will
 * fill in the next time the user runs a sync.
 */
export function isQualityOutdated(
  current: { resolution: VideoMaxResolution; codec: CodecPreference },
  archived: {
    resolution: VideoMaxResolution | null
    codec: CodecPreference | null
  },
): boolean {
  if (!archived.resolution || !archived.codec) return false
  return (
    current.resolution !== archived.resolution ||
    current.codec !== archived.codec
  )
}
