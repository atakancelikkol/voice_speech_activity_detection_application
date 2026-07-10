/*
 * arf_vad.h - Improved energy/SNR based Voice Activity Detector.
 *
 * Standalone extraction of the adaptive VAD from the arf-recog-adaptive-vad
 * UniMRCP plugin (plugins/arf-recog-adaptive-vad/src/arf_vad.[ch]). The
 * detection algorithm is unchanged. Mechanical modifications only:
 *  - apr_pool allocation replaced with malloc/free
 *    (arf_vad_create/arf_vad_destroy)
 *  - apr_size_t replaced with size_t
 *  - mpf_frame_t replaced with a plain (samples, count) pair; the
 *    MEDIA_FRAME_TYPE_AUDIO check became a samples && count check, so a
 *    NULL/empty frame takes the CN/DTX "silence frame" path
 *  - CODEC_FRAME_TIME_BASE inlined as ARF_VAD_FRAME_TIME_BASE
 *  - apt_log/arf_plugin_log includes removed; the log call sites are kept in
 *    arf_vad.c behind a no-op sink; arf_vad_log_ids_set and the per-call log
 *    ids were dropped (they only fed the log prefix)
 *  - arf_vad_reset (full per-call reset) re-exported for host use
 *
 * Drop-in style replacement for UniMRCP's mpf_activity_detector that:
 *   - tracks an adaptive noise floor instead of using a fixed level threshold,
 *   - decides on SNR (dB above the noise floor) with dual onset/offset
 *     thresholds (hysteresis),
 *   - uses a leaky integrator so short dips/pauses do not reset onset/offset,
 *   - removes DC offset and uses the zero-crossing rate to help catch
 *     low-energy unvoiced onsets (s, f, sh...).
 *
 * The emitted events mirror mpf_detector_event_e so the engine only needs to
 * switch on a different enum:
 *   ARF_VAD_EVENT_ACTIVITY    -> start of speech
 *   ARF_VAD_EVENT_INACTIVITY  -> end of speech (trailing silence reached)
 *   ARF_VAD_EVENT_NOINPUT     -> no speech within noinput_timeout
 */

#ifndef ARF_VAD_H
#define ARF_VAD_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/** Frame duration in ms (from UniMRCP CODEC_FRAME_TIME_BASE) */
#define ARF_VAD_FRAME_TIME_BASE 10

/** Opaque detector handle */
typedef struct arf_vad_t arf_vad_t;

/** Events reported by the detector (same semantics as mpf_detector_event_e) */
typedef enum {
    ARF_VAD_EVENT_NONE,        /**< nothing of interest in this frame      */
    ARF_VAD_EVENT_ACTIVITY,    /**< transition silence -> speech (onset)   */
    ARF_VAD_EVENT_INACTIVITY,  /**< transition speech  -> silence (offset) */
    ARF_VAD_EVENT_NOINPUT      /**< no speech detected within noinput_timeout */
} arf_vad_event_e;

/** Create a detector (heap-allocated; free with arf_vad_destroy). */
arf_vad_t* arf_vad_create(void);

/** Destroy a detector created with arf_vad_create. */
void arf_vad_destroy(arf_vad_t *vad);

/** FULL reset (per-call): clears utterance state AND the learned proximity
 * references. */
void arf_vad_reset(arf_vad_t *vad);

/** Reset state (noise floor, timers); call at the start of each RECOGNIZE. */
void arf_vad_reset_utterance(arf_vad_t *vad);

/* ---- Timeout configuration (milliseconds) ---------------------------- */

/** Continuous speech required to confirm an onset (transition->active). */
void arf_vad_speech_timeout_set(arf_vad_t *vad, size_t ms);
/** Trailing silence required to confirm an offset (active->transition->idle). */
void arf_vad_silence_timeout_set(arf_vad_t *vad, size_t ms);
/** Silence-from-start after which ARF_VAD_EVENT_NOINPUT is reported. */
void arf_vad_noinput_timeout_set(arf_vad_t *vad, size_t ms);
/** Duration of one media frame in ms (default ARF_VAD_FRAME_TIME_BASE = 10). */
void arf_vad_frame_duration_set(arf_vad_t *vad, size_t ms);

/* ---- SNR tuning (dB above the tracked noise floor) ------------------- */

/** SNR needed to *enter* the speech-candidate state (default 9 dB). */
void arf_vad_onset_snr_set(arf_vad_t *vad, double db);
/** SNR below which a frame counts as silence (default 5 dB). Must be < onset. */
void arf_vad_offset_snr_set(arf_vad_t *vad, double db);

/** Absolute mean-abs amplitude (0..32767) below which a frame is ALWAYS treated
 * as silence, regardless of the adaptive SNR decision. This is a safety floor
 * that mirrors the stock mpf_activity_detector: it guarantees that genuinely
 * quiet ambient can never latch the detector in the "speech" state when the
 * adaptive noise floor was trained too low (e.g. on comfort-noise/half-duplex
 * silence during TTS, then the live mic engages after a barge-in). Set to 0 to
 * disable the override and rely purely on SNR. Default 120. */
void arf_vad_abs_silence_level_set(arf_vad_t *vad, size_t level);

/* ---- Dominant-talker (proximity) gate -------------------------------- *
 * The near talker on a handset is much LOUDER than background room speech
 * ("babble"), because they speak directly into the mouthpiece. A single-channel
 * speech/noise classifier (libfvad, Silero) cannot separate the two: background
 * babble IS speech, so it votes "speech". The only discriminator left on one
 * mono channel is loudness, and these two (independent) levers exploit it. Both
 * gate the ONSET decision only -- never the offset/hold -- so they can reject a
 * far talker starting a false segment WITHOUT ever chopping the near talker's
 * own quiet tail (that is what offset_snr / abs_silence_level handle). */

/** Absolute mean-abs amplitude (0..32767) a frame must reach to START speech, in
 * addition to the SNR onset gate. Rejects quiet far babble that still clears the
 * SNR onset on a quiet line (where the adaptive floor trained very low). This is
 * a higher, onset-only sibling of abs_silence_level (which is the silence/hold
 * floor). SELF-DISARMING: applies only until the near-talker reference is
 * learned; see the plugin source for the full rationale. 0 disables (default). */
void arf_vad_onset_level_set(arf_vad_t *vad, size_t level);

/** Consecutive qualifying frames a STRICT onset gate must see before it lets the
 * onset through, instead of a single "crossed -> hop" frame. Applies to both the
 * phase-1 absolute floor (onset_level) and the phase-2 adaptive proximity margin;
 * any frame failing the strict condition resets the run. Counted in frames
 * (10 ms each by default), e.g. 3 = 30 ms. 0 disables -> single-frame/legacy
 * behaviour (default). */
void arf_vad_onset_confirm_frames_set(arf_vad_t *vad, size_t frames);

/** High-confidence bypass of the spectral (libfvad) veto, in dB of SNR. A noise
 * vote is IGNORED for any frame whose snr >= @a db. Set well above onset_snr so
 * only clearly-loud speech bypasses. 0 disables -> the spectral veto is always
 * honoured (default). */
void arf_vad_spec_bypass_snr_set(arf_vad_t *vad, double db);

/** Relative proximity gate, in dB. Once near speech has been confirmed the
 * detector tracks the near talker's level; while this is set (>0) any frame more
 * than @a db below that tracked level cannot START a new speech segment, so a
 * quieter background talker in a near-talker pause does not re-onset. 0 disables
 * (default). Typical starting point: ~12-18 dB. */
void arf_vad_dominant_drop_set(arf_vad_t *vad, double db);

/** Adaptive (two-cluster) proximity gate, in dB. While set (>0) the detector
 * tracks a SECOND, background ("far") loudness cluster alongside the near
 * talker, and an onset is blocked unless it STARTS at least @a margin_db ABOVE
 * that learned background level. Onset-only and never blocks the first onset.
 * 0 disables (default). Typical: 6-10 dB. */
void arf_vad_adaptive_proximity_set(arf_vad_t *vad, double margin_db);

/** Enable/disable the zero-crossing-rate assist for unvoiced onsets. */
void arf_vad_zcr_enable(arf_vad_t *vad, int enable);

/* ---- Spectral speech/noise fusion ----------------------------------- */

/** Per-frame spectral speech/noise vote from a trained sub-band classifier
 *  (e.g. libfvad): 1 = speech-like, 0 = noise-like, -1 = unknown/disabled.
 *  Set this BEFORE arf_vad_process for the same frame; it is consumed and reset
 *  to -1 each call so a missing hint never sticks. A noise vote (0) forces the
 *  frame to count as silence regardless of its energy/SNR. A speech vote (1) or
 *  "unknown" (-1) leaves the energy decision untouched. */
void arf_vad_spectral_vote_set(arf_vad_t *vad, int vote);

/** Process one frame of LPCM (16-bit) audio; returns the detected event.
 * A NULL/empty frame is treated as a silence frame (CN/DTX), so the no-input
 * and trailing-silence timers keep advancing. */
arf_vad_event_e arf_vad_process(arf_vad_t *vad, const int16_t *samples, size_t count);

/* ---- Introspection (handy for logging/tuning) ------------------------ */

/** Last frame power in dB (0..~90). */
double arf_vad_last_level_db(const arf_vad_t *vad);
/** Current tracked noise floor in dB. */
double arf_vad_noise_floor_db(const arf_vad_t *vad);
/** Tracked near-talker reference level in dB used by the proximity gate
 * (0 until the first speech is confirmed). */
double arf_vad_speech_ref_db(const arf_vad_t *vad);
/** Tracked background ("far") loudness in dB used by the adaptive proximity gate
 * (0 until a background cluster has been learned, or after it has expired). */
double arf_vad_far_ref_db(const arf_vad_t *vad);
/** Current state-machine stage as a short string: "IDLE", "ONSET", "ACTIVE" or
 * "OFFSET". */
const char* arf_vad_state_str(const arf_vad_t *vad);
/** Milliseconds accumulated in the current transition stage. Only meaningful in
 * ONSET (counting toward speech_timeout) and OFFSET (toward silence_timeout);
 * 0 in IDLE/ACTIVE. */
size_t arf_vad_state_duration_ms(const arf_vad_t *vad);

#ifdef __cplusplus
}
#endif

#endif /* ARF_VAD_H */
