/*
 * rn_denoise -- RNNoise (github.com/xiph/rnnoise) behind the same in-place
 * int16 API as df_enhance, so the engine can switch between the two noise
 * cleaners (ARF_ENH_MODE=df|rnnoise).
 *
 * RNNoise is 48 kHz-native with fixed 480-sample (10 ms) frames. This wrapper
 * owns the telephony adaptation: 8/16 kHz input is taken in 10 ms hops,
 * upsampled x6/x3 with a polyphase windowed-sinc interpolator, denoised at
 * 48 kHz with the vendored little model (see rnnoise/NOTICES), then filtered
 * and decimated back. 48 kHz input passes straight through the model.
 *
 * Latency: one 10 ms hop of ring buffering + RNNoise's own 10 ms frame +
 * the two FIR group delays (~2 ms at 8 kHz); see rn_denoise_latency_samples.
 * No allocation after create() apart from rnnoise_create's own state.
 *
 * NOTE: keep this file and rn_denoise.c byte-identical with the copy in
 *   voice_speech_activity_detection_application/third_party/rnnoise_enh/
 * (unimrcp plugin arf-recog-ten-vad is the reference copy).
 */
#ifndef RN_DENOISE_H
#define RN_DENOISE_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct rn_denoise_t rn_denoise_t;  /* opaque */

/* Allocate (including the RNNoise state). NULL on failure. */
rn_denoise_t *rn_denoise_create(void);

/* Free. NULL-safe. */
void rn_denoise_destroy(rn_denoise_t *rn);

/* (Re)configure for a sample rate: 8000, 16000 or 48000 (any rate where
 * 48000 % rate == 0 and rate % 100 == 0). Resets all state. 0 on success. */
int rn_denoise_init(rn_denoise_t *rn, unsigned sample_rate);

/* Clear runtime state (rings, FIR histories, RNNoise GRU state) but keep the
 * configured rate. Call between utterances. */
void rn_denoise_reset(rn_denoise_t *rn);

/* Denoise count samples IN PLACE. Arbitrary counts; output delayed by
 * rn_denoise_latency_samples(). is_speech is accepted for API symmetry with
 * df_enhance_process and ignored (the network needs no hint). Before init:
 * no-op passthrough. */
void rn_denoise_process(rn_denoise_t *rn, short *samples, size_t count,
                        int is_speech);

/* Wet/dry mix, 0..1 (default 0.8). RNNoise is trained on fullband 48 kHz
 * audio; on upsampled telephony its VAD head is unsure and it sometimes
 * gates real speech. Blending the delay-aligned dry signal bounds that
 * damage: wet w caps the worst-case attenuation at 20*log10(1-w)
 * (-14 dB at the 0.8 default). 1.0 = pure RNNoise output. */
void rn_denoise_wet_set(rn_denoise_t *rn, double wet);

/* Speech probability of the most recent 10 ms frame, straight from the
 * RNNoise VAD output head (0..1; 0 before any frame). */
double rn_denoise_vad_prob(const rn_denoise_t *rn);

/* Total pipeline delay in samples at the configured rate. */
unsigned rn_denoise_latency_samples(const rn_denoise_t *rn);

#ifdef __cplusplus
}
#endif

#endif /* RN_DENOISE_H */
