/*
 * df_enhance.h - DeepFilterNet-structured single-channel speech enhancer
 *                (classical estimators in place of the neural networks).
 *
 * Pipeline (mirrors github.com/Rikorose/DeepFilterNet):
 *
 *   int16 in -> sqrt-Hann STFT (20 ms window / 10 ms hop, zero-padded FFT)
 *     -> Stage 1: ERB-band noise tracking + decision-directed Wiener gain
 *                 (replaces DFN's ERB gain network)
 *     -> Stage 2: causal order-5 complex multi-frame Wiener filter on the
 *                 low-frequency bins (replaces DFN's deep-filtering network)
 *     -> ISTFT overlap-add -> int16 out
 *
 * DFN is 48 kHz-native (960/480, 32 ERB bands, deep filtering up to 5 kHz).
 * This implementation derives the same structure from the sample rate, so the
 * 8 kHz telephony geometry (160/80, FFT 256, ~20 ERB bands, DF up to 2 kHz)
 * and the 16/48 kHz geometries all come out of one code path.
 *
 * Self-contained C: no APR, no external libs beyond libm, a single calloc at
 * create time and no allocation in process() (media-thread safe).
 * Latency is fixed at one window = 2 hops (20 ms) for any input chunking.
 *
 * NOTE: keep this file byte-identical with the copy in
 *   voice_speech_activity_detection_application/third_party/df_enhance/
 * (unimrcp plugin arf-recog-ten-vad is the reference copy).
 */
#ifndef DF_ENHANCE_H
#define DF_ENHANCE_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct df_enhance_t df_enhance_t;  /* opaque; one calloc, ~150 KB */

/* -- lifecycle ------------------------------------------------------------ */

/* Allocate an enhancer. Returns NULL on OOM. Call df_enhance_init() before
 * processing. */
df_enhance_t *df_enhance_create(void);

/* Free an enhancer. NULL-safe. */
void df_enhance_destroy(df_enhance_t *de);

/* (Re)configure the geometry for a sample rate (8000/16000/48000; any rate
 * divisible by 100 up to 48000 works), rebuild window/ERB/FFT tables and
 * reset all runtime state. Returns 0 on success, -1 on an unsupported rate. */
int df_enhance_init(df_enhance_t *de, unsigned sample_rate);

/* Clear runtime state (rings, noise trackers, filter histories) but keep the
 * configured rate and tuning. Call between utterances. */
void df_enhance_reset(df_enhance_t *de);

/* -- processing ----------------------------------------------------------- */

/* Enhance count samples IN PLACE. Any count is accepted; internal rings make
 * the output bit-exact regardless of chunking. Output is the enhanced signal
 * delayed by df_enhance_latency_samples() (leading zeros at stream start).
 * is_speech: coarse external VAD hint; 0 accelerates noise learning. Pass 0
 * when unknown. Before df_enhance_init() the call is a no-op passthrough. */
void df_enhance_process(df_enhance_t *de, short *samples, size_t count,
                        int is_speech);

/* -- tuning (defaults in [ ]; take effect from the next frame) ------------ */

void df_enhance_stage1_enable(df_enhance_t *de, int enable);      /* [1] ERB Wiener stage      */
void df_enhance_stage2_enable(df_enhance_t *de, int enable);      /* [1] deep-filtering stage  */
void df_enhance_gain_floor_db_set(df_enhance_t *de, double db);   /* [-15] min stage-1 gain    */
void df_enhance_gain_exponent_set(df_enhance_t *de, double p);    /* [1.0] Wiener exponent     */
void df_enhance_spp_enable(df_enhance_t *de, int enable);         /* [1] SPP-weighted gain     */
void df_enhance_noise_bias_set(df_enhance_t *de, double b);       /* [1.3] min-stat bias comp  */
void df_enhance_noise_hint_enable(df_enhance_t *de, int enable);  /* [1] use is_speech hint    */
void df_enhance_df_cutoff_hz_set(df_enhance_t *de, double hz);    /* [0 = auto: min(5000, rate/4)] */
void df_enhance_df_alpha_max_set(df_enhance_t *de, double a);     /* [0.8] max stage-2 blend 0..1  */
void df_enhance_df_boost_max_db_set(df_enhance_t *de, double db); /* [6.0] stage-2 boost clamp     */
void df_enhance_bypass_set(df_enhance_t *de, int enable);         /* [0] identity STFT->ISTFT      */

/* -- introspection --------------------------------------------------------- */

double df_enhance_noise_level_db(const df_enhance_t *de); /* mean band noise floor, ~dBFS  */
double df_enhance_mean_gain(const df_enhance_t *de);      /* running mean stage-1 gain 0..1 */
unsigned df_enhance_latency_samples(const df_enhance_t *de); /* == 2*hop == 20 ms          */
unsigned df_enhance_band_count(const df_enhance_t *de);   /* effective ERB bands after init */
unsigned df_enhance_nan_resets(const df_enhance_t *de);   /* stage-2 numerical bin resets   */

/* Copy up to max band-edge bin indices (band b spans [edges[b], edges[b+1])).
 * Returns the number of edges written (band_count + 1). */
unsigned df_enhance_band_edges(const df_enhance_t *de, unsigned *edges,
                               unsigned max);

#ifdef __cplusplus
}
#endif

#endif /* DF_ENHANCE_H */
