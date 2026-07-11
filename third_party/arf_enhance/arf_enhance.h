/*
 * arf_audio_enhance.h - Self-contained single-channel speech enhancer.
 *
 * Cleans the LPCM audio that the recognizer streams to the STT backend so the
 * STT sees speech instead of raw, noisy telephony audio. No external libraries:
 * depends only on APR and the C math library (already linked by the plugin).
 *
 * Processing chain, applied in place to every audio frame during a RECOGNIZE:
 *
 *   int16 -> DC blocker (high-pass) -> STFT spectral subtraction + band-limit
 *         -> overlap-add -> AGC + soft limiter -> int16
 *
 *   - DC blocker: removes the DC offset / low rumble common on telephony lines.
 *   - STFT spectral subtraction: estimates the stationary background-noise
 *     magnitude spectrum per bin (tracked from the non-speech part of the call,
 *     minimum-statistics style so speech bursts cannot inflate it) and applies a
 *     Wiener-like per-bin gain. This is the part that actually removes ambient
 *     noise *while the caller is speaking*. A spectral floor + over-subtraction
 *     factor trade residual noise against "musical noise" artifacts.
 *   - band-limit: zeroes energy outside the speech band (default ~150-3800 Hz),
 *     killing out-of-band hum/hiss for free in the same FFT.
 *   - AGC: brings speech to a consistent level so the STT does not have to cope
 *     with very quiet or very hot lines; gentle, gain frozen on non-speech so it
 *     never pumps up the residual noise.
 *
 * The caller passes an `is_speech` hint (derived from the VAD state). It is used
 * only to gate adaptation (freeze the upward noise tracking and the AGC during
 * confirmed speech); the enhancement itself runs on every frame.
 *
 * Self contained and allocation-free after create: all scratch lives in the
 * handle, sized for the largest supported FFT, so it is real-time safe to call
 * from the media thread.
 */

/*
 * Standalone extraction of arf_audio_enhance from the arf-recog-adaptive-vad
 * UniMRCP plugin (plugins/arf-recog-adaptive-vad/src/arf_audio_enhance.[ch]).
 * The DSP is unchanged. Mechanical modifications only:
 *  - apr.h/apr_pools.h includes replaced with <stddef.h>/<stdint.h>
 *  - apr_int16_t -> int16_t, apr_size_t -> size_t
 *  - apr_pool allocation replaced with calloc/free
 *    (arf_audio_enhance_create takes no pool; arf_audio_enhance_destroy added)
 */

#ifndef ARF_AUDIO_ENHANCE_H
#define ARF_AUDIO_ENHANCE_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/** Opaque enhancer handle */
typedef struct arf_audio_enhance_t arf_audio_enhance_t;

/** Create an enhancer from @a pool. Default chain (see arf_audio_enhance.c
 *  DESIGN NOTE): high-pass de-boom -> high-shelf de-muffle -> pumping-free
 *  leveler -> soft-knee peak limiter. Denoise, legacy AGC and pre-emphasis are
 *  OFF by default (all three were shown to hurt either STT or audio quality). */
arf_audio_enhance_t* arf_audio_enhance_create(void);

/** Destroy an enhancer created with arf_audio_enhance_create. */
void arf_audio_enhance_destroy(arf_audio_enhance_t *ae);

/** (Re)configure for a sampling rate (8000 or 16000) and reset all state.
 *  Call once per RECOGNIZE, when the codec descriptor is known. */
void arf_audio_enhance_init(arf_audio_enhance_t *ae, unsigned int sample_rate);

/** Clear runtime state (noise estimate, filters, FFT buffers, AGC gain). */
void arf_audio_enhance_reset(arf_audio_enhance_t *ae);

/** Process @a count 16-bit samples in place. @a is_speech is the VAD hint
 *  (non-zero while speech is confirmed). Safe to call on every frame. */
void arf_audio_enhance_process(arf_audio_enhance_t *ae,
                               int16_t *samples, size_t count,
                               int is_speech);

/* ---- tuning (all optional; sane defaults set at create) -------------- */

/** Master enable for the spectral-subtraction denoiser (default on). */
void arf_audio_enhance_denoise_enable(arf_audio_enhance_t *ae, int enable);
/** Over-subtraction factor: higher removes more noise but risks artifacts
 *  that can confuse the STT. Default 1.5; useful range ~1.0-2.5. */
void arf_audio_enhance_oversub_set(arf_audio_enhance_t *ae, double beta);
/** Spectral floor (min per-bin gain, 0..1): higher leaves more residual noise
 *  but less musical-noise artifact. Default 0.12. */
void arf_audio_enhance_floor_set(arf_audio_enhance_t *ae, double g);
/** Speech band kept by the FFT band-limit, in Hz (default 150..3800). */
void arf_audio_enhance_band_set(arf_audio_enhance_t *ae, double lo_hz, double hi_hz);

/** Master enable for the AGC (default on). */
void arf_audio_enhance_agc_enable(arf_audio_enhance_t *ae, int enable);
/** Target RMS for the AGC in int16 units (default 2500). */
void arf_audio_enhance_agc_target_set(arf_audio_enhance_t *ae, double rms);
/** Maximum boost the AGC may apply (default 6.0). Limits noise pump-up. */
void arf_audio_enhance_agc_max_gain_set(arf_audio_enhance_t *ae, double g);

/** Non-speech soft-duck: target OUTPUT gain (0..1) applied to frames the VAD
 *  reports as NON-speech, so the frozen AGC gain no longer pumps up inter-word
 *  echo/hiss (the main reason speakerphone audio sounds harsh/pumping after
 *  enhancement). It is a smooth, click-free attenuation -- NOT a hard mute --
 *  with a fast attack so a speech onset is never clipped. @a atten == 1.0
 *  disables it (default; identical behaviour to before). Typical when enabling:
 *  0.3 (~ -10 dB duck). Lower ducks harder; raise toward 1.0 if quiet speech
 *  tails get attenuated. Calibrate on a real speakerphone dump. */
void arf_audio_enhance_nonspeech_atten_set(arf_audio_enhance_t *ae, double atten);

/** High-pass cutoff (Hz) that removes the low-frequency boom/rumble masking
 *  clarity. Replaces the plain DC blocker. Default 120 Hz. */
void arf_audio_enhance_hp_set(arf_audio_enhance_t *ae, double fc_hz);

/** Adaptive high-pass: instead of running the de-boom high-pass unconditionally,
 *  observe the input for the first @a window_ms and keep the high-pass engaged
 *  only if the share of input energy below the cutoff exceeds @a ratio -- i.e.
 *  only when the line actually has low-frequency boom/rumble. On a clean line the
 *  high-pass is dropped after the window so it never colours otherwise-fine
 *  audio. DURING the window the high-pass IS applied (safe: a ~120 Hz HPF barely
 *  touches speech), so a boomy line is cleaned from the first frame and only a
 *  clean line is later relaxed. The "below cutoff" share is measured as the
 *  energy the HPF itself removes (x - y), so it is exactly self-consistent with
 *  what would be filtered. @a window_ms <= 0 -> 5000; @a ratio is clamped to
 *  0..1. @a enable == 0 restores the static always-on behaviour (default). */
void arf_audio_enhance_hp_auto_set(arf_audio_enhance_t *ae, int enable,
                                   double window_ms, double ratio);

/** Adaptive-HPF state: 1 = engaged, 0 = dropped (clean line), -1 = still
 *  observing. Always 1 when the adaptive mode is off. */
int arf_audio_enhance_hp_active(const arf_audio_enhance_t *ae);

/** Measured share (0..1) of input energy below the high-pass cutoff over the
 *  observation window. 0 until the window has elapsed. */
double arf_audio_enhance_hp_low_ratio(const arf_audio_enhance_t *ae);

/** "De-muffle" pre-emphasis: y[n] = x[n] - coef*x[n-1], the standard ASR
 *  front-end lift that restores the weak consonant band (2-4 kHz) of muffled
 *  telephony speech. A true +6 dB/oct tilt -- far more effective than a leaky
 *  one-pole shelf (a "+10 dB" shelf only de-tilted ~+3.7 dB in practice; this
 *  de-tilts ~+13 dB). Artifact-free (it is a fixed linear filter, not noise
 *  removal). @a coef in [0, ~0.97]; default 0.90. 0 disables it. Push toward
 *  0.97 if still muffled; lower toward 0.7 if it sounds thin/harsh.
 *
 *  NOTE: pre-emphasis is now OFF by default and superseded by the high-shelf
 *  below. The reason: pre-emph's +6 dB/oct tilt rises WITHOUT BOUND, so it also
 *  amplifies the top octave (hiss + mu-law quantization noise), which on a quiet
 *  line fills the 3-4 kHz band and collapses the Turkish s/sh contrast. The
 *  high-shelf lifts the same consonant band but PLATEAUS, so it does not over-
 *  boost the noisy top. Keep this only for A/B experiments. */
void arf_audio_enhance_preemph_set(arf_audio_enhance_t *ae, double coef);

/** "De-muffle" high-shelf (RBJ biquad), the new default de-tilt. A TIME-INVARIANT
 *  linear filter: identical gain on every sample, so it can never pump and its
 *  effect is level-INDEPENDENT (unlike pre-emph + AGC it cannot collapse the
 *  s/sh contrast on a quiet line). It lifts everything above @a fc_hz by
 *  @a gain_db and then FLATTENS (a plateau, not an unbounded tilt), so the
 *  intelligibility band (~1.5-3.4 kHz) is raised without piling gain into the
 *  hissy top octave. @a gain_db == 0 disables it. Typical: fc 1800 Hz, +7 dB,
 *  Q 0.707. Raise gain_db toward +9 if still muffled; lower toward +4 if harsh.
 *  Lower fc_hz (~1400) to brighten more of the band; raise (~2200) for a gentler
 *  consonant-only lift. */
void arf_audio_enhance_shelf_set(arf_audio_enhance_t *ae,
                                 double fc_hz, double gain_db, double q);

/** Pumping-free leveler (replaces the freeze-and-apply AGC). A continuous,
 *  envelope-driven gain with fast attack / slow release that is computed and
 *  applied PER SAMPLE -- there is no "freeze gain on non-speech then keep
 *  multiplying" step, which was the root cause of the inter-word pumping/echo
 *  boost on reverberant (speakerphone) lines. It is also VAD-INDEPENDENT (does
 *  not use the is_speech hint), so a wrong VAD decision cannot make it pump.
 *  Below @a floor_rms the gain is expanded DOWNWARD (quiet inter-word residue is
 *  attenuated, never amplified). Default ON. */
void arf_audio_enhance_leveler_enable(arf_audio_enhance_t *ae, int enable);
/** Configure the leveler: @a target_rms = desired output level (int16 units,
 *  default 3000 ~ -20 dBFS); @a max_gain = hard cap on boost (default 3.0, keep
 *  <=4 so quiet noise is never blown up); @a floor_rms = envelope below which the
 *  downward expander kicks in (default 200, ~ the abs_silence level). */
void arf_audio_enhance_leveler_set(arf_audio_enhance_t *ae,
                                   double target_rms, double max_gain,
                                   double floor_rms);

/** Output limiter mode: 0 = soft-knee peak limiter (default; LINEAR below ~0.9
 *  FS, gentle compression only into the last headroom, so it adds no broadband
 *  distortion), 1 = legacy memoryless tanh (distorts the whole signal even below
 *  clipping -- kept only for A/B). */
void arf_audio_enhance_limiter_mode_set(arf_audio_enhance_t *ae, int mode);

/* ---- introspection (for diagnostic logging) -------------------------- */

/** Estimated ambient-noise level (linear RMS, int16 units), tracked from the
 *  non-speech frames. Higher = noisier line. 0 until the first frame. */
double arf_audio_enhance_noise_rms(const arf_audio_enhance_t *ae);
/** Mean per-bin denoise gain applied over speech so far (0..1). 1.0 means the
 *  denoiser barely attenuated; lower means it removed more noise. */
double arf_audio_enhance_avg_denoise_gain(const arf_audio_enhance_t *ae);
/** Current AGC gain (1.0 = unity). High values mean the speech was quiet and
 *  got boosted (which also amplifies whatever noise survived the denoise). */
double arf_audio_enhance_agc_gain(const arf_audio_enhance_t *ae);

#ifdef __cplusplus
}
#endif

#endif /* ARF_AUDIO_ENHANCE_H */
