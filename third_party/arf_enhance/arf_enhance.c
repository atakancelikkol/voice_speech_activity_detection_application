/*
 * arf_audio_enhance.c - Self-contained single-channel speech enhancer.
 * See arf_audio_enhance.h for the rationale and public contract.
 *
 * DESIGN NOTE (2026-06-28 redesign: only time-INVARIANT tools by default):
 *   Every earlier attempt to enhance this 8 kHz/16-bit telephony audio used
 *   time-VARYING, blind, per-frame DSP and each aggressive stage broke something:
 *     - spectral-subtraction denoise injected "musical noise" the ASR is not
 *       trained on (HURT recognition);
 *     - pre-emphasis (an UNBOUNDED +6 dB/oct tilt) over-boosted the hissy top
 *       octave on a quiet line and collapsed the Turkish s/sh contrast;
 *     - the AGC froze its gain on non-speech yet kept multiplying, so inter-word
 *       echo/hiss was pumped up (the harsh "bozuk ses" on speakerphone), which
 *       the memoryless tanh limiter then distorted further.
 *   The fix is to use stages that CANNOT pump or shift the spectrum in a level-
 *   dependent way. The default chain is now:
 *       int16 -> high-pass de-boom -> high-shelf de-muffle (RBJ biquad, a fixed
 *               linear filter) -> pumping-free leveler (continuous, per-sample,
 *               VAD-independent, downward-expands quiet residue) -> soft-knee
 *               peak limiter (linear below ~0.9 FS) -> int16
 *   The high-shelf lifts the consonant band like pre-emph but PLATEAUS, so it
 *   does not pile gain into the noisy top. The leveler computes and applies gain
 *   per sample with no freeze step, so it cannot pump between words.
 *
 *   The STFT denoiser, the legacy freeze-and-apply AGC and pre-emphasis are all
 *   still available (arf_audio_enhance_denoise_enable / agc_enable / preemph_set)
 *   but default OFF; keep them off unless an A/B shows they help on your line.
 */

#include "arf_enhance.h"
#include <stdlib.h>
#include <math.h>
#include <string.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

/* ----------------------------------------------------------------------
 * Sizes. The STFT uses a ~32 ms window: 256 bins @ 8 kHz, 512 @ 16 kHz.
 * Hop is 50% (sqrt-Hann WOLA satisfies COLA, so overlap-add needs no scaling).
 * -------------------------------------------------------------------- */
#define ARF_AE_MAX_FFT   512
#define ARF_AE_MAX_HOP   (ARF_AE_MAX_FFT/2)
#define ARF_AE_IN_CAP    (ARF_AE_MAX_FFT + 256)
#define ARF_AE_OUT_CAP   (ARF_AE_MAX_FFT * 8)

/* per-bin noise tracker: fast down / slow up, upward frozen during speech */
#define NF_DOWN_COEF   0.40
#define NF_UP_COEF     0.04

/* decision-directed a priori SNR smoothing (Ephraim-Malah). ~0.98 = smooth. */
#define DD_ALPHA       0.98

#define NOISE_WARMUP_FRAMES  3
#define DC_BLOCK_R     0.995

struct arf_audio_enhance_t {
    /* config */
    unsigned int sample_rate;
    int          n;
    int          hop;
    int          lo_bin;
    int          hi_bin;
    int          denoise_on;     /* default OFF (see design note) */
    int          agc_on;         /* default ON                    */
    double       oversub;        /* legacy over-subtraction factor (Wiener floor scale) */
    double       floor_gain;     /* min per-bin gain (spectral floor) */
    double       band_lo_hz;
    double       band_hi_hz;
    double       agc_target;     /* target RMS (int16 units) */
    double       agc_max_gain;

    /* de-muffle: telephony high-pass (de-boom) + pre-emphasis (consonant lift).
     * Pre-emphasis y[n]=x[n]-c*x[n-1] is the standard ASR front-end de-muffle:
     * a true +6 dB/oct lift, far more effective than the old leaky one-pole
     * shelf, which only delivered a fraction of its nominal boost. */
    double       hp_fc;          /* high-pass cutoff (Hz)            */
    double       preemph_coef;   /* pre-emphasis coef (0..~0.97)     */
    double       hp_a;           /* precomputed high-pass coef      */

    /* adaptive high-pass: observe the first hp_window_samples, then keep the
     * de-boom high-pass only if the sub-cutoff energy share crosses the
     * threshold (boomy line); otherwise drop it (clean line). */
    int          hp_auto;            /* 0 = static always-on; 1 = measure+decide */
    int          hp_engaged;         /* current decision: apply HP? (1 in window) */
    int          hp_decided;         /* observation window elapsed?               */
    double       hp_window_ms;       /* observation length (ms)                   */
    double       hp_window_samples;  /* observation length (samples @ sample_rate)*/
    double       hp_ratio_thresh;    /* sub-cutoff energy share to keep HP on     */
    double       hp_obs_samples;     /* samples observed so far                   */
    double       hp_low_energy;      /* accumulated sub-cutoff energy (x-y)^2     */
    double       hp_tot_energy;      /* accumulated total energy x^2              */
    double       hp_low_ratio;       /* measured sub-cutoff share (diagnostic)    */

    /* sqrt-Hann analysis/synthesis window */
    double       win[ARF_AE_MAX_FFT];

    /* high-pass filter history (one-pole) */
    double       hp_x_prev;
    double       hp_y_prev;
    /* pre-emphasis previous input sample (persists across calls) */
    double       preemph_prev;

    /* high-shelf de-muffle (RBJ biquad, Direct Form I). Time-invariant: fixed
     * gain on every sample, so it can never pump and is level-independent. */
    int          sh_on;          /* gain_db != 0 -> 1 */
    double       sh_fc;          /* corner frequency (Hz)  */
    double       sh_gain_db;     /* shelf gain (dB)        */
    double       sh_q;           /* shelf Q (~0.707)       */
    double       sh_b0, sh_b1, sh_b2, sh_a1, sh_a2;  /* normalized coeffs */
    double       sh_x1, sh_x2, sh_y1, sh_y2;         /* DF-I history      */

    /* pumping-free leveler: continuous envelope-driven gain (replaces AGC).
     * Computed AND applied per sample (no freeze step), VAD-independent, with a
     * downward expander below floor_rms so quiet inter-word residue is never
     * boosted. This is what removes the speakerphone "pumping" without a duck. */
    int          lv_on;
    double       lv_target;      /* desired output RMS (int16 units)            */
    double       lv_max_gain;    /* hard cap on boost                           */
    double       lv_floor_rms;   /* RMS below which to expand downward           */
    double       lv_ms;          /* smoothed mean-square (RMS detector state)   */
    double       lv_gain;        /* smoothed applied gain (persists)            */
    double       lv_noise;       /* VAD-free tracked RMS noise floor (min-stat) */
    int          lv_primed;      /* snap gain once the level has settled         */
    int          lv_hold;        /* consecutive above-gate samples (prime delay) */

    /* output limiter: 0 = soft-knee peak (linear below knee), 1 = legacy tanh */
    int          lim_mode;

    /* DC-blocked scratch for the current call (both paths read it) */
    double       dcbuf[ARF_AE_IN_CAP];

    /* STFT staging (denoise path only) */
    double       inbuf[ARF_AE_IN_CAP];
    int          in_len;
    double       ola[ARF_AE_MAX_HOP];
    double       outring[ARF_AE_OUT_CAP];
    int          out_head;
    int          out_count;

    /* per-call emission scratch */
    double       emit[ARF_AE_IN_CAP];

    /* spectral state (denoise path) */
    double       noise_mag[ARF_AE_MAX_FFT/2 + 1];
    double       prev_gain[ARF_AE_MAX_FFT/2 + 1];
    int          noise_frames;

    /* FFT scratch */
    double       fre[ARF_AE_MAX_FFT];
    double       fim[ARF_AE_MAX_FFT];

    /* AGC */
    double       agc_gain;
    int          agc_primed;     /* snap gain on the first speech frame */

    /* non-speech soft-duck: smoothly attenuates the OUTPUT of non-speech frames
     * instead of passing them at the frozen (possibly 6x) AGC gain. On a noisy/
     * reverberant line (e.g. speakerphone) the frozen AGC gain otherwise pumps up
     * inter-word echo/hiss, which pre-emphasis brightens and the tanh limiter then
     * distorts. A gentle duck (NOT a hard mute) removes that without chopping the
     * speech onset. nonspeech_atten == 1.0 disables it (identical to before). */
    double       nonspeech_atten; /* target output gain on non-speech (0..1; 1=off) */
    double       duck_gain;       /* smoothed live duck multiplier (persists)       */

    /* diagnostics */
    double       noise_rms;
    double       gain_accum;
    long         gain_frames;
};

/* ---- lifecycle ------------------------------------------------------- */

void arf_audio_enhance_destroy(arf_audio_enhance_t *ae)
{
    free(ae);
}

arf_audio_enhance_t* arf_audio_enhance_create(void)
{
    arf_audio_enhance_t *ae = (arf_audio_enhance_t *)calloc(1, sizeof(*ae));
    if(!ae) {
        return NULL;
    }
    memset(ae, 0, sizeof(*ae));
    ae->denoise_on   = 0;        /* OFF by default: see design note */
    ae->agc_on       = 0;        /* legacy freeze-and-apply AGC OFF (leveler replaces it) */
    ae->oversub      = 1.0;
    ae->floor_gain   = 0.15;
    ae->band_lo_hz   = 150.0;
    ae->band_hi_hz   = 3800.0;
    ae->agc_target   = 3000.0;   /* ~ -20 dBFS RMS: healthy without clipping */
    ae->agc_max_gain = 6.0;
    ae->hp_fc        = 120.0;             /* remove sub-120 Hz boom */
    ae->preemph_coef = 0.0;               /* OFF: superseded by the high-shelf */
    ae->nonspeech_atten = 1.0;            /* OFF: the leveler's expander handles this */
    ae->hp_auto      = 0;                 /* static always-on HPF by default */
    ae->hp_window_ms = 5000.0;            /* adaptive observation window (ms) */
    ae->hp_ratio_thresh = 0.30;           /* keep HPF if >30% energy below cutoff */

    /* high-shelf de-muffle ON by default (bounded consonant lift) */
    ae->sh_fc        = 1800.0;
    ae->sh_gain_db   = 7.0;
    ae->sh_q         = 0.707;
    ae->sh_on        = 1;

    /* pumping-free leveler ON by default */
    ae->lv_on        = 1;
    ae->lv_target    = 3000.0;
    ae->lv_max_gain  = 3.0;
    ae->lv_floor_rms = 200.0;

    ae->lim_mode     = 0;                 /* soft-knee peak limiter */

    arf_audio_enhance_init(ae, 8000);
    return ae;
}

/* RBJ "Audio EQ Cookbook" high-shelf, normalized to a0 and stored for a
 * Direct-Form-I difference equation:
 *   y = b0*x + b1*x1 + b2*x2 - a1*y1 - a2*y2
 * gain_db == 0 -> identity (sh_on cleared). */
static void arf_ae_calc_shelf(arf_audio_enhance_t *ae)
{
    double A, w0, cw, sw, alpha, two_sqrtA_alpha, a0;

    if(ae->sh_gain_db == 0.0 || ae->sh_fc <= 0.0) {
        ae->sh_on = 0;
        ae->sh_b0 = 1.0; ae->sh_b1 = ae->sh_b2 = ae->sh_a1 = ae->sh_a2 = 0.0;
        return;
    }
    ae->sh_on = 1;
    A  = pow(10.0, ae->sh_gain_db / 40.0);
    w0 = 2.0 * M_PI * ae->sh_fc / (double)ae->sample_rate;
    cw = cos(w0);
    sw = sin(w0);
    alpha = sw / (2.0 * (ae->sh_q > 0.0 ? ae->sh_q : 0.707));
    two_sqrtA_alpha = 2.0 * sqrt(A) * alpha;

    a0          =        (A + 1.0) - (A - 1.0) * cw + two_sqrtA_alpha;
    ae->sh_b0   =   A * ((A + 1.0) + (A - 1.0) * cw + two_sqrtA_alpha) / a0;
    ae->sh_b1   = -2.0 * A * ((A - 1.0) + (A + 1.0) * cw)             / a0;
    ae->sh_b2   =   A * ((A + 1.0) + (A - 1.0) * cw - two_sqrtA_alpha) / a0;
    ae->sh_a1   =  2.0 * ((A - 1.0) - (A + 1.0) * cw)                 / a0;
    ae->sh_a2   =       ((A + 1.0) - (A - 1.0) * cw - two_sqrtA_alpha) / a0;
}

void arf_audio_enhance_init(arf_audio_enhance_t *ae, unsigned int sample_rate)
{
    int i;
    double nyq, hi;

    ae->sample_rate = sample_rate ? sample_rate : 8000;
    ae->n   = (ae->sample_rate > 8000) ? 512 : 256;
    ae->hop = ae->n / 2;

    for(i = 0; i < ae->n; i++) {
        double h = 0.5 * (1.0 - cos(2.0 * M_PI * i / ae->n));
        ae->win[i] = sqrt(h);
    }

    nyq = ae->sample_rate / 2.0;
    hi  = ae->band_hi_hz;
    if(hi > nyq * 0.95) hi = nyq * 0.95;
    ae->lo_bin = (int)floor(ae->band_lo_hz * ae->n / ae->sample_rate);
    ae->hi_bin = (int)ceil(hi * ae->n / ae->sample_rate);
    if(ae->lo_bin < 0) ae->lo_bin = 0;
    if(ae->hi_bin > ae->n / 2) ae->hi_bin = ae->n / 2;

    /* precompute the one-pole de-boom high-pass coefficient */
    ae->hp_a = exp(-2.0 * M_PI * ae->hp_fc / ae->sample_rate);

    /* precompute the high-shelf de-muffle coefficients for this rate */
    arf_ae_calc_shelf(ae);

    /* adaptive-HPF observation window in samples at the current rate */
    ae->hp_window_samples = ae->hp_window_ms * ae->sample_rate / 1000.0;

    arf_audio_enhance_reset(ae);
}

void arf_audio_enhance_reset(arf_audio_enhance_t *ae)
{
    int k;
    ae->hp_x_prev = 0.0;
    ae->hp_y_prev = 0.0;
    ae->preemph_prev = 0.0;
    /* high-shelf DF-I history */
    ae->sh_x1 = ae->sh_x2 = ae->sh_y1 = ae->sh_y2 = 0.0;
    /* leveler: start at unity so the first frames are not ducked or boosted.
     * Seed the noise tracker at the floor knob (capped) so a clip that starts
     * mid-word does not latch the noise estimate onto speech. */
    ae->lv_ms     = 0.0;
    ae->lv_gain   = 1.0;
    ae->lv_noise  = ae->lv_floor_rms;
    ae->lv_primed = 0;
    ae->lv_hold   = 0;
    /* adaptive HPF: engaged during the observation window (and always, if the
     * adaptive mode is off); decision (re)made fresh each utterance. */
    ae->hp_engaged    = 1;
    ae->hp_decided    = 0;
    ae->hp_obs_samples = 0.0;
    ae->hp_low_energy = 0.0;
    ae->hp_tot_energy = 0.0;
    ae->hp_low_ratio  = 0.0;
    ae->in_len    = 0;
    ae->out_head  = 0;
    ae->out_count = 0;
    ae->noise_frames = 0;
    ae->agc_gain  = 1.0;
    ae->agc_primed = 0;
    ae->duck_gain = 1.0;         /* start un-ducked so the first frames are clean */
    ae->noise_rms = 0.0;
    ae->gain_accum = 0.0;
    ae->gain_frames = 0;
    memset(ae->ola, 0, sizeof(ae->ola));
    memset(ae->noise_mag, 0, sizeof(ae->noise_mag));
    for(k = 0; k <= ARF_AE_MAX_FFT/2; k++) ae->prev_gain[k] = 1.0; /* Wiener neutral */
}

/* ---- setters --------------------------------------------------------- */

void arf_audio_enhance_denoise_enable(arf_audio_enhance_t *ae, int enable) { ae->denoise_on = enable ? 1 : 0; }
void arf_audio_enhance_oversub_set(arf_audio_enhance_t *ae, double beta)   { ae->oversub = beta; }
void arf_audio_enhance_floor_set(arf_audio_enhance_t *ae, double g)        { ae->floor_gain = g; }
void arf_audio_enhance_agc_enable(arf_audio_enhance_t *ae, int enable)     { ae->agc_on = enable ? 1 : 0; }
void arf_audio_enhance_agc_target_set(arf_audio_enhance_t *ae, double rms) { ae->agc_target = rms; }
void arf_audio_enhance_agc_max_gain_set(arf_audio_enhance_t *ae, double g) { ae->agc_max_gain = g; }

void arf_audio_enhance_nonspeech_atten_set(arf_audio_enhance_t *ae, double atten)
{
    if(atten < 0.0) atten = 0.0;
    if(atten > 1.0) atten = 1.0;
    ae->nonspeech_atten = atten;
}

void arf_audio_enhance_band_set(arf_audio_enhance_t *ae, double lo_hz, double hi_hz)
{
    ae->band_lo_hz = lo_hz;
    ae->band_hi_hz = hi_hz;
    arf_audio_enhance_init(ae, ae->sample_rate);
}

void arf_audio_enhance_hp_set(arf_audio_enhance_t *ae, double fc_hz)
{
    ae->hp_fc = fc_hz;
    arf_audio_enhance_init(ae, ae->sample_rate); /* recompute coef */
}

void arf_audio_enhance_hp_auto_set(arf_audio_enhance_t *ae, int enable,
                                   double window_ms, double ratio)
{
    if(window_ms <= 0.0) window_ms = 5000.0;
    if(ratio < 0.0) ratio = 0.0;
    if(ratio > 1.0) ratio = 1.0;
    ae->hp_auto         = enable ? 1 : 0;
    ae->hp_window_ms    = window_ms;
    ae->hp_ratio_thresh = ratio;
    arf_audio_enhance_init(ae, ae->sample_rate); /* recompute window + reset */
}

void arf_audio_enhance_preemph_set(arf_audio_enhance_t *ae, double coef)
{
    if(coef < 0.0)  coef = 0.0;
    if(coef > 0.99) coef = 0.99;
    ae->preemph_coef = coef;
}

void arf_audio_enhance_shelf_set(arf_audio_enhance_t *ae,
                                 double fc_hz, double gain_db, double q)
{
    ae->sh_fc      = fc_hz;
    ae->sh_gain_db = gain_db;
    ae->sh_q       = (q > 0.0) ? q : 0.707;
    arf_ae_calc_shelf(ae);      /* recompute coeffs for the current rate */
    ae->sh_x1 = ae->sh_x2 = ae->sh_y1 = ae->sh_y2 = 0.0;
}

void arf_audio_enhance_leveler_enable(arf_audio_enhance_t *ae, int enable)
{
    ae->lv_on = enable ? 1 : 0;
}

void arf_audio_enhance_leveler_set(arf_audio_enhance_t *ae,
                                   double target_rms, double max_gain,
                                   double floor_rms)
{
    if(target_rms < 1.0)   target_rms = 1.0;
    if(max_gain   < 1.0)   max_gain   = 1.0;
    if(floor_rms  < 0.0)   floor_rms  = 0.0;
    ae->lv_target    = target_rms;
    ae->lv_max_gain  = max_gain;
    ae->lv_floor_rms = floor_rms;
}

void arf_audio_enhance_limiter_mode_set(arf_audio_enhance_t *ae, int mode)
{
    ae->lim_mode = (mode == 1) ? 1 : 0;
}

/* ---- introspection --------------------------------------------------- */

double arf_audio_enhance_noise_rms(const arf_audio_enhance_t *ae)
{
    return ae ? ae->noise_rms : 0.0;
}
double arf_audio_enhance_avg_denoise_gain(const arf_audio_enhance_t *ae)
{
    if(!ae || ae->gain_frames <= 0) return 1.0;
    return ae->gain_accum / (double)ae->gain_frames;
}
double arf_audio_enhance_agc_gain(const arf_audio_enhance_t *ae)
{
    if(!ae) return 1.0;
    /* report whichever leveler is active so the heartbeat shows the live gain */
    return ae->lv_on ? ae->lv_gain : ae->agc_gain;
}
int arf_audio_enhance_hp_active(const arf_audio_enhance_t *ae)
{
    if(!ae) return 0;
    if(!ae->hp_auto)    return 1;   /* static: always on */
    if(!ae->hp_decided) return -1;  /* still observing    */
    return ae->hp_engaged;
}
double arf_audio_enhance_hp_low_ratio(const arf_audio_enhance_t *ae)
{
    return ae ? ae->hp_low_ratio : 0.0;
}

/* ---- iterative radix-2 FFT (in place) -------------------------------- */
static void arf_ae_fft(double *re, double *im, int n, int inverse)
{
    int i, j, len, k;

    for(i = 1, j = 0; i < n; i++) {
        int bit = n >> 1;
        for(; j & bit; bit >>= 1) j ^= bit;
        j ^= bit;
        if(i < j) {
            double tr = re[i]; re[i] = re[j]; re[j] = tr;
            double ti = im[i]; im[i] = im[j]; im[j] = ti;
        }
    }
    for(len = 2; len <= n; len <<= 1) {
        double ang = (inverse ? 2.0 : -2.0) * M_PI / len;
        double wr = cos(ang), wi = sin(ang);
        int half = len >> 1;
        for(i = 0; i < n; i += len) {
            double cwr = 1.0, cwi = 0.0;
            for(k = 0; k < half; k++) {
                double br = re[i + k + half], bi = im[i + k + half];
                double vr = br * cwr - bi * cwi;
                double vi = br * cwi + bi * cwr;
                double ar = re[i + k], ai = im[i + k];
                double ncwr;
                re[i + k]        = ar + vr;
                im[i + k]        = ai + vi;
                re[i + k + half] = ar - vr;
                im[i + k + half] = ai - vi;
                ncwr = cwr * wr - cwi * wi;
                cwi  = cwr * wi + cwi * wr;
                cwr  = ncwr;
            }
        }
    }
    if(inverse) {
        for(i = 0; i < n; i++) { re[i] /= n; im[i] /= n; }
    }
}

/* ---- one STFT hop: decision-directed Wiener denoise ------------------ */
static void arf_ae_process_hop(arf_audio_enhance_t *ae, int is_speech)
{
    int n = ae->n, half = n / 2, k, i;
    double gsum = 0.0; int gcnt = 0;

    for(i = 0; i < n; i++) {
        ae->fre[i] = ae->inbuf[i] * ae->win[i];
        ae->fim[i] = 0.0;
    }
    arf_ae_fft(ae->fre, ae->fim, n, 0);

    if(ae->noise_frames < 1000000) ae->noise_frames++;

    for(k = 0; k <= half; k++) {
        double re = ae->fre[k], im = ae->fim[k];
        double mag = sqrt(re * re + im * im);
        double g = 1.0;

        /* track background-noise magnitude (fast down, slow up, frozen on speech) */
        if(ae->noise_frames == 1) {
            ae->noise_mag[k] = mag;
        }
        else if(mag < ae->noise_mag[k]) {
            ae->noise_mag[k] += NF_DOWN_COEF * (mag - ae->noise_mag[k]);
        }
        else if(!is_speech) {
            ae->noise_mag[k] += NF_UP_COEF * (mag - ae->noise_mag[k]);
        }

        if(ae->noise_frames >= NOISE_WARMUP_FRAMES) {
            double npow = ae->noise_mag[k] * ae->noise_mag[k] * ae->oversub + 1.0;
            double gamma = (mag * mag) / npow;               /* a posteriori SNR */
            double xi = DD_ALPHA * ae->prev_gain[k] * ae->prev_gain[k] * gamma
                      + (1.0 - DD_ALPHA) * (gamma > 1.0 ? gamma - 1.0 : 0.0);
            g = xi / (1.0 + xi);                             /* Wiener gain      */
            if(g < ae->floor_gain) g = ae->floor_gain;
            if(g > 1.0) g = 1.0;
        }
        ae->prev_gain[k] = g;

        if(k >= ae->lo_bin && k <= ae->hi_bin) { gsum += g; gcnt++; }
        if(k < ae->lo_bin || k > ae->hi_bin) g = 0.0;

        ae->fre[k] *= g;
        ae->fim[k] *= g;
        if(k > 0 && k < half) {
            ae->fre[n - k] *= g;
            ae->fim[n - k] *= g;
        }
    }

    if(is_speech && gcnt > 0) { ae->gain_accum += gsum / (double)gcnt; ae->gain_frames++; }

    arf_ae_fft(ae->fre, ae->fim, n, 1);

    for(i = 0; i < half; i++) {
        double v = ae->ola[i] + ae->fre[i] * ae->win[i];
        if(ae->out_count < ARF_AE_OUT_CAP) {
            int pos = (ae->out_head + ae->out_count) % ARF_AE_OUT_CAP;
            ae->outring[pos] = v;
            ae->out_count++;
        }
    }
    for(i = 0; i < half; i++) {
        ae->ola[i] = ae->fre[i + half] * ae->win[i + half];
    }

    memmove(ae->inbuf, ae->inbuf + half, (ae->in_len - half) * sizeof(double));
    ae->in_len -= half;
}

/* ---- main entry ------------------------------------------------------ */

void arf_audio_enhance_process(arf_audio_enhance_t *ae,
                               int16_t *samples, size_t count,
                               int is_speech)
{
    size_t i;
    double in_sumsq = 0.0;
    double sumsq = 0.0;
    double rms, frame_gain;

    if(!ae || !samples || count == 0) return;
    if(count > ARF_AE_IN_CAP) return;   /* media frames are tiny (<=160) */

    /* 1) telephony high-pass (de-boom) into dcbuf; track ambient noise level.
     *    The HP output y is always computed (its energy gap x-y is the sub-cutoff
     *    "boom" the adaptive mode measures). When the adaptive mode has decided
     *    the line is clean (hp_engaged==0) we pass the raw x through instead. */
    for(i = 0; i < count; i++) {
        double x = (double)samples[i];
        double y = ae->hp_a * (ae->hp_y_prev + x - ae->hp_x_prev);
        ae->hp_x_prev = x;
        ae->hp_y_prev = y;
        in_sumsq += x * x;

        if(ae->hp_auto && !ae->hp_decided) {
            double lp = x - y;                 /* energy the HPF would remove */
            ae->hp_low_energy += lp * lp;
            ae->hp_tot_energy += x * x;
            ae->hp_obs_samples += 1.0;
            if(ae->hp_obs_samples >= ae->hp_window_samples) {
                ae->hp_low_ratio = (ae->hp_tot_energy > 0.0)
                                 ? ae->hp_low_energy / ae->hp_tot_energy : 0.0;
                ae->hp_engaged   = (ae->hp_low_ratio >= ae->hp_ratio_thresh) ? 1 : 0;
                ae->hp_decided   = 1;
            }
        }

        ae->dcbuf[i] = ae->hp_engaged ? y : x;
    }
    {
        double in_rms = sqrt(in_sumsq / (double)count);
        if(!is_speech) {
            if(ae->noise_rms <= 0.0)        ae->noise_rms = in_rms;
            else if(in_rms < ae->noise_rms) ae->noise_rms += 0.30 * (in_rms - ae->noise_rms);
            else                            ae->noise_rms += 0.05 * (in_rms - ae->noise_rms);
        }
    }

    /* 2) fill emit[] either via the STFT denoiser or straight through */
    if(ae->denoise_on) {
        for(i = 0; i < count; i++)
            if(ae->in_len < ARF_AE_IN_CAP) ae->inbuf[ae->in_len++] = ae->dcbuf[i];
        while(ae->in_len >= ae->n) arf_ae_process_hop(ae, is_speech);
        for(i = 0; i < count; i++) {
            double v = 0.0;            /* zeros only during initial priming */
            if(ae->out_count > 0) {
                v = ae->outring[ae->out_head];
                ae->out_head = (ae->out_head + 1) % ARF_AE_OUT_CAP;
                ae->out_count--;
            }
            ae->emit[i] = v;
        }
    }
    else {
        /* clean path: high-passed signal straight through (no latency/artifacts) */
        for(i = 0; i < count; i++) ae->emit[i] = ae->dcbuf[i];
    }

    /* 2b) de-muffle (legacy, OFF by default): pre-emphasis y[n]=x[n]-c*x[n-1].
     *     Superseded by the high-shelf below because its tilt is unbounded and
     *     over-boosts the hissy top octave. Kept only for A/B. */
    if(ae->preemph_coef > 0.0) {
        for(i = 0; i < count; i++) {
            double xv = ae->emit[i];
            ae->emit[i] = xv - ae->preemph_coef * ae->preemph_prev;
            ae->preemph_prev = xv;
        }
    }

    /* 2c) de-muffle (default): high-shelf RBJ biquad. Time-invariant => the same
     *     gain on every sample, level-INDEPENDENT, so it can never pump and can
     *     never collapse the s/sh contrast the way pre-emph+AGC did on quiet
     *     lines. It lifts the consonant band (above sh_fc) and then PLATEAUS,
     *     instead of tilting up without bound into the noise. */
    if(ae->sh_on) {
        for(i = 0; i < count; i++) {
            double x = ae->emit[i];
            double y = ae->sh_b0 * x + ae->sh_b1 * ae->sh_x1 + ae->sh_b2 * ae->sh_x2
                     - ae->sh_a1 * ae->sh_y1 - ae->sh_a2 * ae->sh_y2;
            ae->sh_x2 = ae->sh_x1; ae->sh_x1 = x;
            ae->sh_y2 = ae->sh_y1; ae->sh_y1 = y;
            ae->emit[i] = y;
        }
    }

    /* 3) level. Default: the pumping-free leveler -- a continuous, per-sample,
     *    VAD-INDEPENDENT gain (envelope follower + downward expander below the
     *    floor). It writes the scaled signal straight into emit[] and leaves
     *    frame_gain at unity. Because the gain is recomputed every sample there
     *    is no "freeze on non-speech then keep multiplying" step, so it cannot
     *    pump inter-word echo/hiss the way the legacy AGC did. The legacy AGC
     *    stays as an opt-in alternative (arf_audio_enhance_agc_enable). */
    if(ae->lv_on) {
        /* Two SEPARATE gains so the leveler and the gate do not fight:
         *   lv_gain  - the AGC level gain. Slow, and updated ONLY while a real
         *              signal is present (above the gate); HELD across pauses so
         *              when speech resumes it is already at the right level
         *              (this is what stopped speech being attenuated after each
         *              pause when the expander and leveler were one gain).
         *   duck_gain- a fast gate applied on top: 1.0 on signal, <1 on inter-word
         *              residue. Un-ducks FAST so a speech onset is never clipped,
         *              ducks a bit slower so tails fade naturally.
         * one-pole rates: tau_ms ~= 1000/(k*rate). */
        const double k_ms  = 0.004;                  /* RMS detector (~30 ms)    */
        const double g_atk = 0.003,  g_rel = 0.0004; /* leveler: down ~40, up ~400ms */
        const double d_up  = 0.05,   d_dn  = 0.006;  /* gate: un-duck ~2.5, duck ~20ms */
        const int    prime_hold = ae->sample_rate * 30 / 1000; /* prime after ~30ms above gate */
        for(i = 0; i < count; i++) {
            double x = ae->emit[i];
            double rmsq, gate, duck_t, kd;
            /* RMS level detector (true RMS, not a peak-biased |x| envelope, so
             * speech sits AT target instead of being turned down by peaks). */
            ae->lv_ms += k_ms * (x * x - ae->lv_ms);
            rmsq = sqrt(ae->lv_ms > 0.0 ? ae->lv_ms : 0.0);
            /* VAD-free noise-floor tracker (min-statistics): fall fast, rise very
             * slowly, so it sits on the inter-word background, not on speech. */
            if(rmsq < ae->lv_noise) ae->lv_noise += 0.02   * (rmsq - ae->lv_noise);
            else                    ae->lv_noise += 0.0003 * (rmsq - ae->lv_noise);
            gate = 3.0 * ae->lv_noise;               /* ~ +9.5 dB above the floor */
            if(gate < 1.0) gate = 1.0;

            if(rmsq >= gate) {
                /* real signal: update (or prime) the held leveler gain */
                double g_des = ae->lv_target / (rmsq > 1.0 ? rmsq : 1.0);
                if(g_des > ae->lv_max_gain) g_des = ae->lv_max_gain;
                if(g_des < 0.25)            g_des = 0.25;
                /* Prime only AFTER ~30 ms above the gate, so the snap uses the
                 * SETTLED level, not the still-rising onset. Priming on the very
                 * first crossing (rmsq low) snapped the gain to max, which a hot
                 * line then had to slowly attack back down -- overshooting into
                 * the limiter. Until primed the gain stays ~unity, which neither
                 * over-boosts a loud start nor clips it. */
                if(!ae->lv_primed) {
                    if(++ae->lv_hold >= prime_hold) { ae->lv_gain = g_des; ae->lv_primed = 1; }
                } else {
                    double kg = (g_des < ae->lv_gain) ? g_atk : g_rel;
                    ae->lv_gain += kg * (g_des - ae->lv_gain);
                }
                duck_t = 1.0;
            }
            else {
                /* below the gate: suppress the inter-word residue (downward
                 * expand toward the tracked floor) but HOLD the leveler gain. */
                if(!ae->lv_primed) ae->lv_hold = 0;  /* require a sustained onset */
                duck_t = rmsq / gate;                /* 0..1 */
            }
            kd = (duck_t > ae->duck_gain) ? d_up : d_dn;
            ae->duck_gain += kd * (duck_t - ae->duck_gain);

            ae->emit[i] = x * ae->lv_gain * ae->duck_gain;
        }
        frame_gain = 1.0;
    }
    else {
        /* legacy AGC: smooth, speech-gated, snapped to target on the first
         * speech frame, frozen on non-speech (can pump -- see DESIGN NOTE). */
        for(i = 0; i < count; i++) sumsq += ae->emit[i] * ae->emit[i];
        rms = sqrt(sumsq / (double)count);
        if(ae->agc_on && is_speech && rms > 80.0) {
            double desired = ae->agc_target / rms;
            if(desired > ae->agc_max_gain) desired = ae->agc_max_gain;
            if(desired < 0.25) desired = 0.25;
            if(!ae->agc_primed) { ae->agc_gain = desired; ae->agc_primed = 1; }
            else                  ae->agc_gain += 0.10 * (desired - ae->agc_gain);
        }
        frame_gain = ae->agc_on ? ae->agc_gain : 1.0;
    }

    /* 4) apply the (legacy) frame gain + optional non-speech duck, then limit and
     *    write int16. With the leveler active emit[] is already at level
     *    (frame_gain==1) and the duck is bypassed (the leveler's expander already
     *    handled non-speech). The default soft-knee limiter is LINEAR below ~0.9
     *    FS and only saturates into the last headroom, so unlike the memoryless
     *    tanh it adds no broadband distortion. */
    {
        const double duck_target = (is_speech ? 1.0 : ae->nonspeech_atten);
        const double k_up = 0.05, k_down = 0.005;   /* per-sample one-pole rates */
        const int duck_on = (!ae->lv_on && ae->nonspeech_atten < 1.0);
        const double knee = 0.90 * 32767.0;
        const double range = 32767.0 - knee;        /* headroom above the knee */
        for(i = 0; i < count; i++) {
            double y, lim;
            long s;
            if(duck_on) {
                double k = (duck_target > ae->duck_gain) ? k_up : k_down;
                ae->duck_gain += k * (duck_target - ae->duck_gain);
            }
            y = ae->emit[i] * frame_gain * (duck_on ? ae->duck_gain : 1.0);
            if(ae->lim_mode == 1) {
                lim = 32767.0 * tanh(y / 32767.0);  /* legacy memoryless tanh */
            }
            else {
                double ay = (y < 0.0) ? -y : y;
                if(ay <= knee) {
                    lim = y;                         /* linear below the knee */
                }
                else {
                    double mag = knee + range * tanh((ay - knee) / range);
                    lim = (y < 0.0) ? -mag : mag;
                }
            }
            s = (long)(lim < 0.0 ? lim - 0.5 : lim + 0.5);
            if(s >  32767) s =  32767;
            if(s < -32768) s = -32768;
            samples[i] = (int16_t)s;
        }
    }
}
