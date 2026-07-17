/*
 * df_enhance.c - DeepFilterNet-structured speech enhancer, classical edition.
 * See df_enhance.h for the overview and the copy-sync note.
 *
 * Geometry per rate (win = 20 ms, hop = 10 ms, FFT = next pow2 of win):
 *
 *   rate  win  hop  FFT   bins  ERB bands  DF bins (auto cutoff)
 *   8000  160   80  256    129  ~21        64  (2000 Hz)
 *  16000  320  160  512    257  ~25        128 (4000 Hz)
 *  48000  960  480  1024   513   32        106 (5000 Hz)
 */
#include "df_enhance.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

#define DF_MAX_FFT     1024
#define DF_MAX_BINS    (DF_MAX_FFT / 2 + 1)          /* 513 */
#define DF_MAX_WIN     960
#define DF_MAX_HOP     480
#define DF_MAX_BANDS   40
#define DF_MF_ORDER    5                              /* deep-filter taps    */
#define DF_HERM        (DF_MF_ORDER * (DF_MF_ORDER + 1) / 2)  /* 15 packed   */
#define DF_MAX_DF_BINS 128
#define DF_WARMUP_FRAMES 20                           /* stage-2 blend hold  */
#define DF_EPS         1e-3f
#define DF_COV_SEED    1e2f                           /* Ry/Rn diagonal seed */

/* Stage-1 recursion constants (10 ms frames) */
#define DF_ALPHA_P     0.85f    /* periodogram smoothing (~60 ms)            */
#define DF_DOB_G       0.998f   /* Doblinger minimum-tracking pole           */
#define DF_DOB_B       0.96f    /* Doblinger slope constant                  */
#define DF_HINT_A      0.05f    /* hinted noise learning (~200 ms)           */
#define DF_DD_ALPHA    0.96f    /* decision-directed a-priori SNR weight     */
#define DF_XI_MIN      0.0031622776601683794f         /* -25 dB              */
#define DF_G_RELEASE   0.6f     /* max per-frame gain drop (~4.4 dB)         */

/* Stage-2 recursion constants */
#define DF_LAMBDA_Y    0.88f    /* noisy covariance smoothing (~80 ms)       */
#define DF_LAMBDA_N    0.97f    /* noise covariance smoothing (~330 ms)      */
#define DF_SPP_NOISE   0.3f     /* band SPP below which frames count as noise*/
#define DF_LOAD_FRAC   0.01     /* diagonal loading: frac of mean Ry diag    */

struct df_enhance_t {
    /* geometry */
    unsigned rate;              /* 0 until init */
    int W, hop, N, K;           /* window, hop, FFT size, bins (N/2+1)       */
    int nb;                     /* effective ERB band count                  */
    int k_df;                   /* deep filtering applied to bins [1, k_df)  */

    /* tuning */
    int stage1_on, stage2_on, spp_on, hint_on, bypass;
    float gain_floor;           /* linear */
    float gain_exp;
    float noise_bias;
    float df_cutoff_hz;         /* 0 = auto */
    float df_alpha_max;
    float df_boost_max;         /* linear */

    /* I/O rings (int16 domain) */
    short in_buf[DF_MAX_HOP];
    int   in_fill;
    short out_buf[2 * DF_MAX_HOP];
    int   out_fill;
    int   hint;                 /* latest is_speech hint */

    /* STFT machinery */
    float ana[DF_MAX_WIN];      /* sliding analysis buffer (last W samples)  */
    float win[DF_MAX_WIN];      /* sqrt-Hann, analysis == synthesis          */
    float ola[DF_MAX_FFT];      /* overlap-add accumulator                   */
    float fft_re[DF_MAX_FFT], fft_im[DF_MAX_FFT];
    float tw_re[DF_MAX_FFT / 2], tw_im[DF_MAX_FFT / 2];
    unsigned short brev[DF_MAX_FFT];

    /* spectra */
    float Xre[DF_MAX_BINS], Xim[DF_MAX_BINS];     /* noisy                   */
    float X1re[DF_MAX_BINS], X1im[DF_MAX_BINS];   /* after stage 1           */
    float gain_bin[DF_MAX_BINS];
    float pp_bin[DF_MAX_BINS];

    /* ERB filterbank */
    int   edge[DF_MAX_BANDS + 1];   /* band b covers bins [edge[b],edge[b+1]) */
    float center[DF_MAX_BANDS];     /* band center in (fractional) bins       */
    unsigned char interp_b[DF_MAX_BINS];  /* bin -> lower band of interp pair */
    float         interp_w[DF_MAX_BINS];  /* weight of upper band             */

    /* stage-1 per-band state */
    float Pb[DF_MAX_BANDS], Pb_prev[DF_MAX_BANDS];
    float Nb[DF_MAX_BANDS];
    float Gb[DF_MAX_BANDS], Yb_prev[DF_MAX_BANDS];
    float ppb[DF_MAX_BANDS];

    /* stage-2 per-bin state (bins 1..k_df-1 use slots [bin]) */
    float hist_re[DF_MAX_DF_BINS][DF_MF_ORDER - 1];  /* past X1, newest first */
    float hist_im[DF_MAX_DF_BINS][DF_MF_ORDER - 1];
    float Ry_re[DF_MAX_DF_BINS][DF_HERM];  /* packed lower tri, (i,j) i>=j    */
    float Ry_im[DF_MAX_DF_BINS][DF_HERM];
    float Rn_re[DF_MAX_DF_BINS][DF_HERM];
    float Rn_im[DF_MAX_DF_BINS][DF_HERM];

    /* diagnostics */
    unsigned long frame_counter;
    unsigned nan_resets;
    float mean_gain;            /* slow running mean of the band-mean gain   */
};

/* ---- small helpers ------------------------------------------------------- */

static float df_erb_scale(float hz)
{
    return 21.4f * (float)log10(1.0 + 0.00437 * hz);
}

static float df_erb_inv(float e)
{
    return (float)((pow(10.0, e / 21.4) - 1.0) / 0.00437);
}

static int df_next_pow2(int v)
{
    int n = 1;
    while(n < v) n <<= 1;
    return n;
}

/* packed Hermitian lower-triangle index, i >= j */
#define DF_IDX(i, j) ((i) * ((i) + 1) / 2 + (j))

/* ---- FFT (iterative radix-2, precomputed twiddles) ----------------------- */

static void df_fft(df_enhance_t *de, float *re, float *im, int inverse)
{
    const int n = de->N;
    int i, len;
    for(i = 0; i < n; i++) {
        int j = de->brev[i];
        if(i < j) {
            float t;
            t = re[i]; re[i] = re[j]; re[j] = t;
            t = im[i]; im[i] = im[j]; im[j] = t;
        }
    }
    for(len = 2; len <= n; len <<= 1) {
        const int half = len >> 1, step = n / len;
        for(i = 0; i < n; i += len) {
            int k;
            for(k = 0; k < half; k++) {
                const float wr = de->tw_re[k * step];
                const float wi = inverse ? -de->tw_im[k * step]
                                         :  de->tw_im[k * step];
                const int a = i + k, b = i + k + half;
                const float br = re[b] * wr - im[b] * wi;
                const float bi = re[b] * wi + im[b] * wr;
                re[b] = re[a] - br; im[b] = im[a] - bi;
                re[a] += br;        im[a] += bi;
            }
        }
    }
    if(inverse) {
        const float s = 1.0f / (float)n;
        for(i = 0; i < n; i++) { re[i] *= s; im[i] *= s; }
    }
}

/* ---- lifecycle ----------------------------------------------------------- */

df_enhance_t *df_enhance_create(void)
{
    df_enhance_t *de = (df_enhance_t *)calloc(1, sizeof(*de));
    if(!de) return NULL;
    de->stage1_on    = 1;
    de->stage2_on    = 1;
    de->spp_on       = 1;
    de->hint_on      = 1;
    de->bypass       = 0;
    de->gain_floor   = (float)pow(10.0, -15.0 / 20.0);
    de->gain_exp     = 1.0f;
    de->noise_bias   = 1.3f;
    de->df_cutoff_hz = 0.0f;    /* auto */
    de->df_alpha_max = 0.8f;
    de->df_boost_max = (float)pow(10.0, 6.0 / 20.0);
    return de;
}

void df_enhance_destroy(df_enhance_t *de)
{
    if(de) free(de);
}

void df_enhance_reset(df_enhance_t *de)
{
    int k, b;
    if(!de) return;
    de->in_fill  = 0;
    de->hint     = 0;
    memset(de->in_buf,  0, sizeof(de->in_buf));
    memset(de->out_buf, 0, sizeof(de->out_buf));
    /* pre-seed one hop of zeros so arbitrary chunkings always have output
     * available; combined with the one-hop OLA delay this fixes the total
     * latency at 2*hop for every feeding pattern. */
    de->out_fill = de->hop;
    memset(de->ana, 0, sizeof(de->ana));
    memset(de->ola, 0, sizeof(de->ola));
    for(b = 0; b < DF_MAX_BANDS; b++) {
        de->Pb[b] = de->Pb_prev[b] = 0.0f;
        de->Nb[b] = 0.0f;
        de->Gb[b] = 1.0f;
        de->Yb_prev[b] = 0.0f;
        de->ppb[b] = 0.0f;
    }
    memset(de->hist_re, 0, sizeof(de->hist_re));
    memset(de->hist_im, 0, sizeof(de->hist_im));
    memset(de->Ry_re, 0, sizeof(de->Ry_re));
    memset(de->Ry_im, 0, sizeof(de->Ry_im));
    memset(de->Rn_re, 0, sizeof(de->Rn_re));
    memset(de->Rn_im, 0, sizeof(de->Rn_im));
    for(k = 0; k < DF_MAX_DF_BINS; k++) {
        int i;
        for(i = 0; i < DF_MF_ORDER; i++) {
            de->Ry_re[k][DF_IDX(i, i)] = DF_COV_SEED;
            de->Rn_re[k][DF_IDX(i, i)] = DF_COV_SEED;
        }
    }
    de->frame_counter = 0;
    de->nan_resets = 0;
    de->mean_gain = 1.0f;
}

int df_enhance_init(df_enhance_t *de, unsigned sample_rate)
{
    int n, b, k;
    float erb_total, step_erb, cutoff;
    int nb_nom;

    if(!de) return -1;
    if(sample_rate < 8000 || sample_rate > 48000 || (sample_rate % 100) != 0)
        return -1;

    de->rate = sample_rate;
    de->W    = (int)(sample_rate / 50);   /* 20 ms */
    de->hop  = de->W / 2;                 /* 10 ms */
    de->N    = df_next_pow2(de->W);
    de->K    = de->N / 2 + 1;

    /* sqrt-Hann (periodic): analysis * synthesis = Hann, and Hann at 50%
     * overlap sums to exactly 1 -> perfect WOLA reconstruction. */
    for(n = 0; n < de->W; n++) {
        float h = 0.5f - 0.5f * (float)cos(2.0 * M_PI * n / de->W);
        de->win[n] = (float)sqrt(h);
    }

    /* FFT tables */
    for(n = 0; n < de->N; n++) {
        int j = 0, bit, i = n;
        for(bit = de->N >> 1; bit; bit >>= 1, i >>= 1)
            j = (j << 1) | (i & 1);
        de->brev[n] = (unsigned short)j;
    }
    for(n = 0; n < de->N / 2; n++) {
        de->tw_re[n] = (float)cos(-2.0 * M_PI * n / de->N);
        de->tw_im[n] = (float)sin(-2.0 * M_PI * n / de->N);
    }

    /* ERB band edges: keep DFN's band density (32 bands over 48 kHz) and
     * derive the count for this rate's Nyquist -> ~21 bands at 8 kHz. */
    erb_total = df_erb_scale(sample_rate / 2.0f);
    step_erb  = df_erb_scale(24000.0f) / 32.0f;
    nb_nom    = (int)ceil(erb_total / step_erb);
    if(nb_nom < 8) nb_nom = 8;
    if(nb_nom > DF_MAX_BANDS) nb_nom = DF_MAX_BANDS;

    de->edge[0] = 0;
    de->nb = 0;
    for(b = 1; b <= nb_nom; b++) {
        float f = df_erb_inv(erb_total * (float)b / (float)nb_nom);
        int e = (int)floor(f * de->N / de->rate + 0.5);
        if(e < de->edge[de->nb] + 2)
            e = de->edge[de->nb] + 2;          /* min band width: 2 bins */
        if(e >= de->K || b == nb_nom) {
            de->edge[de->nb + 1] = de->K;      /* last band ends at Nyquist */
            de->nb++;
            break;
        }
        de->edge[de->nb + 1] = e;
        de->nb++;
    }
    for(b = 0; b < de->nb; b++)
        de->center[b] = 0.5f * (float)(de->edge[b] + de->edge[b + 1] - 1);

    /* bin -> band interpolation table (linear between band centers) */
    for(k = 0; k < de->K; k++) {
        if((float)k <= de->center[0]) {
            de->interp_b[k] = 0; de->interp_w[k] = 0.0f;
        }
        else if((float)k >= de->center[de->nb - 1]) {
            de->interp_b[k] = (unsigned char)(de->nb - 2 >= 0 ? de->nb - 2 : 0);
            de->interp_w[k] = 1.0f;
        }
        else {
            for(b = 0; b < de->nb - 1; b++) {
                if((float)k >= de->center[b] && (float)k < de->center[b + 1]) {
                    de->interp_b[k] = (unsigned char)b;
                    de->interp_w[k] = ((float)k - de->center[b]) /
                                      (de->center[b + 1] - de->center[b]);
                    break;
                }
            }
        }
    }

    /* deep-filtering cutoff: DFN filters up to 5 kHz at 48 k; scale down for
     * narrowband (min(5000, rate/4) -> 2 kHz at 8 k). */
    cutoff = de->df_cutoff_hz > 0.0f ? de->df_cutoff_hz
                                     : (de->rate / 4.0f < 5000.0f ?
                                        de->rate / 4.0f : 5000.0f);
    de->k_df = (int)(cutoff * de->N / de->rate);
    if(de->k_df > DF_MAX_DF_BINS) de->k_df = DF_MAX_DF_BINS;
    if(de->k_df > de->K - 1)      de->k_df = de->K - 1;

    df_enhance_reset(de);
    return 0;
}

/* ---- tuning setters ------------------------------------------------------ */

void df_enhance_stage1_enable(df_enhance_t *de, int enable)
{ if(de) de->stage1_on = enable ? 1 : 0; }

void df_enhance_stage2_enable(df_enhance_t *de, int enable)
{ if(de) de->stage2_on = enable ? 1 : 0; }

void df_enhance_gain_floor_db_set(df_enhance_t *de, double db)
{ if(de) { if(db > 0.0) db = 0.0; de->gain_floor = (float)pow(10.0, db / 20.0); } }

void df_enhance_gain_exponent_set(df_enhance_t *de, double p)
{ if(de) { if(p < 0.25) p = 0.25; if(p > 4.0) p = 4.0; de->gain_exp = (float)p; } }

void df_enhance_spp_enable(df_enhance_t *de, int enable)
{ if(de) de->spp_on = enable ? 1 : 0; }

void df_enhance_noise_bias_set(df_enhance_t *de, double b)
{ if(de) { if(b < 1.0) b = 1.0; if(b > 4.0) b = 4.0; de->noise_bias = (float)b; } }

void df_enhance_noise_hint_enable(df_enhance_t *de, int enable)
{ if(de) de->hint_on = enable ? 1 : 0; }

void df_enhance_df_cutoff_hz_set(df_enhance_t *de, double hz)
{
    if(!de) return;
    de->df_cutoff_hz = hz > 0.0 ? (float)hz : 0.0f;
    if(de->rate) {
        float cutoff = de->df_cutoff_hz > 0.0f ? de->df_cutoff_hz
                                               : (de->rate / 4.0f < 5000.0f ?
                                                  de->rate / 4.0f : 5000.0f);
        de->k_df = (int)(cutoff * de->N / de->rate);
        if(de->k_df > DF_MAX_DF_BINS) de->k_df = DF_MAX_DF_BINS;
        if(de->k_df > de->K - 1)      de->k_df = de->K - 1;
    }
}

void df_enhance_df_alpha_max_set(df_enhance_t *de, double a)
{ if(de) { if(a < 0.0) a = 0.0; if(a > 1.0) a = 1.0; de->df_alpha_max = (float)a; } }

void df_enhance_df_boost_max_db_set(df_enhance_t *de, double db)
{ if(de) { if(db < 0.0) db = 0.0; de->df_boost_max = (float)pow(10.0, db / 20.0); } }

void df_enhance_bypass_set(df_enhance_t *de, int enable)
{ if(de) de->bypass = enable ? 1 : 0; }

/* ---- introspection ------------------------------------------------------- */

double df_enhance_noise_level_db(const df_enhance_t *de)
{
    double sum = 0.0;
    int b;
    if(!de || !de->nb) return -120.0;
    for(b = 0; b < de->nb; b++) sum += de->Nb[b];
    sum /= de->nb;
    /* normalize by full-scale and the analysis window power (sum win^2 = W/2)
     * to get an approximate dBFS reading */
    sum /= 32768.0 * 32768.0 * (de->W / 2.0);
    return 10.0 * log10(sum + 1e-12);
}

double df_enhance_mean_gain(const df_enhance_t *de)
{ return de ? de->mean_gain : 1.0; }

unsigned df_enhance_latency_samples(const df_enhance_t *de)
{ return de ? (unsigned)(2 * de->hop) : 0; }

unsigned df_enhance_band_count(const df_enhance_t *de)
{ return de ? (unsigned)de->nb : 0; }

unsigned df_enhance_nan_resets(const df_enhance_t *de)
{ return de ? de->nan_resets : 0; }

unsigned df_enhance_band_edges(const df_enhance_t *de, unsigned *edges,
                               unsigned max)
{
    unsigned i, n;
    if(!de || !edges || !de->nb) return 0;
    n = (unsigned)de->nb + 1;
    if(n > max) n = max;
    for(i = 0; i < n; i++) edges[i] = (unsigned)de->edge[i];
    return n;
}

/* ---- stage 1: ERB-band noise tracking + DD Wiener gain ------------------- */

static void df_stage1(df_enhance_t *de)
{
    int b, k;
    float gsum = 0.0f;

    for(b = 0; b < de->nb; b++) {
        float Yb = 0.0f, Pb_old, gamma, xi, Gw, pp, G;
        int lo = de->edge[b], hi = de->edge[b + 1];
        for(k = lo; k < hi; k++)
            Yb += de->Xre[k] * de->Xre[k] + de->Xim[k] * de->Xim[k];
        Yb /= (float)(hi - lo);
        if(Yb < DF_EPS) Yb = DF_EPS;

        Pb_old = de->Pb[b];
        if(de->frame_counter == 0) {
            de->Pb[b] = Yb;
            de->Nb[b] = Yb;
            Pb_old = Yb;
        }
        else if(de->frame_counter < 10) {
            /* fast convergence during the very first 100 ms */
            de->Pb[b] = 0.7f * de->Pb[b] + 0.3f * Yb;
            de->Nb[b] = de->Pb[b];
        }
        else {
            de->Pb[b] = DF_ALPHA_P * de->Pb[b] + (1.0f - DF_ALPHA_P) * Yb;
            /* Doblinger continuous minimum tracking */
            if(de->Pb[b] >= de->Nb[b]) {
                de->Nb[b] = DF_DOB_G * de->Nb[b] +
                            ((1.0f - DF_DOB_G) / (1.0f - DF_DOB_B)) *
                            (de->Pb[b] - DF_DOB_B * Pb_old);
                if(de->Nb[b] > de->Pb[b]) de->Nb[b] = de->Pb[b];
            }
            else {
                de->Nb[b] = de->Pb[b];
            }
            /* external VAD hint: learn faster while known non-speech */
            if(de->hint_on && !de->hint && de->Pb[b] < 4.0f * de->Nb[b])
                de->Nb[b] = (1.0f - DF_HINT_A) * de->Nb[b] + DF_HINT_A * de->Pb[b];
        }
        if(de->Nb[b] < DF_EPS) de->Nb[b] = DF_EPS;
        de->Pb_prev[b] = Pb_old;

        /* decision-directed a-priori SNR */
        gamma = Yb / (de->noise_bias * de->Nb[b]);
        xi = DF_DD_ALPHA * (de->Gb[b] * de->Gb[b] * de->Yb_prev[b]) /
                 (de->noise_bias * de->Nb[b]) +
             (1.0f - DF_DD_ALPHA) * (gamma > 1.0f ? gamma - 1.0f : 0.0f);
        if(xi < DF_XI_MIN) xi = DF_XI_MIN;

        Gw = xi / (1.0f + xi);
        if(de->gain_exp != 1.0f) Gw = (float)pow(Gw, de->gain_exp);

        /* speech presence probability: sigmoid over xi in dB
         * (0.5 at -3 dB, ~0.95 at +6 dB) */
        pp = 1.0f / (1.0f + (float)exp(-(10.0 * log10(xi) + 3.0) / 3.0));
        de->ppb[b] = pp;

        G = de->spp_on ? pp * Gw + (1.0f - pp) * de->gain_floor : Gw;
        if(G < DF_G_RELEASE * de->Gb[b]) G = DF_G_RELEASE * de->Gb[b];
        if(G < de->gain_floor) G = de->gain_floor;
        if(G > 1.0f) G = 1.0f;

        de->Gb[b] = G;
        de->Yb_prev[b] = Yb;
        gsum += G;
    }
    de->mean_gain = 0.99f * de->mean_gain + 0.01f * (gsum / (float)de->nb);

    /* band gains/SPP -> per-bin via linear interpolation between centers */
    for(k = 0; k < de->K; k++) {
        int bl = de->interp_b[k];
        int bu = bl + 1 < de->nb ? bl + 1 : bl;
        float w = de->interp_w[k];
        de->gain_bin[k] = (1.0f - w) * de->Gb[bl] + w * de->Gb[bu];
        de->pp_bin[k]   = (1.0f - w) * de->ppb[bl] + w * de->ppb[bu];
    }

    if(de->stage1_on) {
        for(k = 0; k < de->K; k++) {
            de->X1re[k] = de->gain_bin[k] * de->Xre[k];
            de->X1im[k] = de->gain_bin[k] * de->Xim[k];
        }
    }
    else {
        memcpy(de->X1re, de->Xre, de->K * sizeof(float));
        memcpy(de->X1im, de->Xim, de->K * sizeof(float));
    }
}

/* ---- stage 2: causal order-5 multi-frame Wiener ("deep filtering") ------- */

static void df_bin_reset(df_enhance_t *de, int k)
{
    int i;
    memset(de->hist_re[k], 0, sizeof(de->hist_re[k]));
    memset(de->hist_im[k], 0, sizeof(de->hist_im[k]));
    memset(de->Ry_re[k], 0, sizeof(de->Ry_re[k]));
    memset(de->Ry_im[k], 0, sizeof(de->Ry_im[k]));
    memset(de->Rn_re[k], 0, sizeof(de->Rn_re[k]));
    memset(de->Rn_im[k], 0, sizeof(de->Rn_im[k]));
    for(i = 0; i < DF_MF_ORDER; i++) {
        de->Ry_re[k][DF_IDX(i, i)] = DF_COV_SEED;
        de->Rn_re[k][DF_IDX(i, i)] = DF_COV_SEED;
    }
    de->nan_resets++;
}

/* Filter one bin; returns the stage-2 spectrum in ore/oim.
 * yv[] holds [current, past1, .., past4] of the stage-1 output. */
static void df_stage2_bin(df_enhance_t *de, int k, const float *yv_re,
                          const float *yv_im, float *ore, float *oim)
{
    double L_re[DF_HERM], L_im[DF_HERM];  /* Cholesky factor, packed lower */
    double z_re[DF_MF_ORDER], z_im[DF_MF_ORDER];
    double u_re[DF_MF_ORDER], u_im[DF_MF_ORDER];
    double h_re[DF_MF_ORDER], h_im[DF_MF_ORDER];
    double d, trace, hn, yr, yi, mag2, ref;
    float alpha, ppk;
    int i, j, m;

    /* diagonal loading proportional to the mean noisy-covariance diagonal */
    trace = 0.0;
    for(i = 0; i < DF_MF_ORDER; i++) trace += de->Ry_re[k][DF_IDX(i, i)];
    d = DF_LOAD_FRAC * trace / DF_MF_ORDER + 1e-9;

    /* Cholesky of A = Ry + d*I (Hermitian, packed lower triangle) */
    for(j = 0; j < DF_MF_ORDER; j++) {
        double s = de->Ry_re[k][DF_IDX(j, j)] + d;
        for(m = 0; m < j; m++)
            s -= L_re[DF_IDX(j, m)] * L_re[DF_IDX(j, m)] +
                 L_im[DF_IDX(j, m)] * L_im[DF_IDX(j, m)];
        if(s <= 1e-12 || s != s) {           /* not PD despite loading */
            df_bin_reset(de, k);
            *ore = yv_re[0]; *oim = yv_im[0];
            return;
        }
        L_re[DF_IDX(j, j)] = sqrt(s);
        L_im[DF_IDX(j, j)] = 0.0;
        for(i = j + 1; i < DF_MF_ORDER; i++) {
            double sr = de->Ry_re[k][DF_IDX(i, j)];
            double si = de->Ry_im[k][DF_IDX(i, j)];
            if(i == j) si = 0.0;
            for(m = 0; m < j; m++) {
                /* s -= L(i,m) * conj(L(j,m)) */
                sr -= L_re[DF_IDX(i, m)] * L_re[DF_IDX(j, m)] +
                      L_im[DF_IDX(i, m)] * L_im[DF_IDX(j, m)];
                si -= L_im[DF_IDX(i, m)] * L_re[DF_IDX(j, m)] -
                      L_re[DF_IDX(i, m)] * L_im[DF_IDX(j, m)];
            }
            L_re[DF_IDX(i, j)] = sr / L_re[DF_IDX(j, j)];
            L_im[DF_IDX(i, j)] = si / L_re[DF_IDX(j, j)];
        }
    }

    /* b = Rn e1 (first column of Rn); forward solve L z = b */
    for(i = 0; i < DF_MF_ORDER; i++) {
        double br = de->Rn_re[k][DF_IDX(i, 0)];
        double bi = i == 0 ? 0.0 : de->Rn_im[k][DF_IDX(i, 0)];
        for(m = 0; m < i; m++) {
            br -= L_re[DF_IDX(i, m)] * z_re[m] - L_im[DF_IDX(i, m)] * z_im[m];
            bi -= L_re[DF_IDX(i, m)] * z_im[m] + L_im[DF_IDX(i, m)] * z_re[m];
        }
        z_re[i] = br / L_re[DF_IDX(i, i)];
        z_im[i] = bi / L_re[DF_IDX(i, i)];
    }
    /* backward solve L^H u = z  (L^H(i,m) = conj(L(m,i)), m >= i) */
    for(i = DF_MF_ORDER - 1; i >= 0; i--) {
        double sr = z_re[i], si = z_im[i];
        for(m = i + 1; m < DF_MF_ORDER; m++) {
            /* subtract conj(L(m,i)) * u(m) */
            sr -= L_re[DF_IDX(m, i)] * u_re[m] + L_im[DF_IDX(m, i)] * u_im[m];
            si -= L_re[DF_IDX(m, i)] * u_im[m] - L_im[DF_IDX(m, i)] * u_re[m];
        }
        u_re[i] = sr / L_re[DF_IDX(i, i)];
        u_im[i] = si / L_re[DF_IDX(i, i)];
    }

    /* h = e1 - u; output = h^H y = sum conj(h_i) y_i */
    hn = 0.0;
    for(i = 0; i < DF_MF_ORDER; i++) {
        h_re[i] = (i == 0 ? 1.0 : 0.0) - u_re[i];
        h_im[i] = -u_im[i];
        hn += h_re[i] * h_re[i] + h_im[i] * h_im[i];
    }
    yr = yi = 0.0;
    for(i = 0; i < DF_MF_ORDER; i++) {
        yr += h_re[i] * yv_re[i] + h_im[i] * yv_im[i];
        yi += h_re[i] * yv_im[i] - h_im[i] * yv_re[i];
    }
    if(hn > 4.0) {                            /* noise-amplification clamp */
        double s = 2.0 / sqrt(hn);
        yr *= s; yi *= s;
    }

    /* boost clamp relative to the stage-1 magnitude */
    mag2 = yr * yr + yi * yi;
    ref = (double)de->df_boost_max *
          sqrt((double)yv_re[0] * yv_re[0] + (double)yv_im[0] * yv_im[0]);
    if(mag2 > ref * ref && mag2 > 0.0) {
        double s = ref / sqrt(mag2);
        yr *= s; yi *= s;
    }

    /* SPP-weighted blend with the stage-1 spectrum; hold off during warmup */
    ppk = de->pp_bin[k];
    alpha = de->frame_counter < DF_WARMUP_FRAMES ? 0.0f
                                                 : de->df_alpha_max * ppk;
    *ore = (1.0f - alpha) * yv_re[0] + alpha * (float)yr;
    *oim = (1.0f - alpha) * yv_im[0] + alpha * (float)yi;

    if(!(*ore == *ore) || !(*oim == *oim) ||
       *ore > 1e18f || *ore < -1e18f || *oim > 1e18f || *oim < -1e18f) {
        df_bin_reset(de, k);
        *ore = yv_re[0]; *oim = yv_im[0];
    }
}

static void df_stage2(df_enhance_t *de)
{
    int k, i, j;
    for(k = 1; k < de->k_df; k++) {
        float yv_re[DF_MF_ORDER], yv_im[DF_MF_ORDER];
        int noise_frame;
        yv_re[0] = de->X1re[k];
        yv_im[0] = de->X1im[k];
        for(i = 1; i < DF_MF_ORDER; i++) {
            yv_re[i] = de->hist_re[k][i - 1];
            yv_im[i] = de->hist_im[k][i - 1];
        }

        /* covariance recursions: Ry always, Rn on (likely) noise-only frames.
         * Only the internal SPP gates Rn: the external VAD hint lags speech
         * onsets (~250 ms), and counting onset frames as noise would teach
         * the deep filter to cancel them. */
        noise_frame = (de->pp_bin[k] < DF_SPP_NOISE);
        for(i = 0; i < DF_MF_ORDER; i++) {
            for(j = 0; j <= i; j++) {
                /* y_i * conj(y_j) */
                float pr = yv_re[i] * yv_re[j] + yv_im[i] * yv_im[j];
                float pi = yv_im[i] * yv_re[j] - yv_re[i] * yv_im[j];
                int x = DF_IDX(i, j);
                de->Ry_re[k][x] = DF_LAMBDA_Y * de->Ry_re[k][x] +
                                  (1.0f - DF_LAMBDA_Y) * pr;
                de->Ry_im[k][x] = DF_LAMBDA_Y * de->Ry_im[k][x] +
                                  (1.0f - DF_LAMBDA_Y) * pi;
                if(noise_frame) {
                    de->Rn_re[k][x] = DF_LAMBDA_N * de->Rn_re[k][x] +
                                      (1.0f - DF_LAMBDA_N) * pr;
                    de->Rn_im[k][x] = DF_LAMBDA_N * de->Rn_im[k][x] +
                                      (1.0f - DF_LAMBDA_N) * pi;
                }
            }
        }

        df_stage2_bin(de, k, yv_re, yv_im, &de->X1re[k], &de->X1im[k]);

        /* shift history (newest first) */
        for(i = DF_MF_ORDER - 2; i > 0; i--) {
            de->hist_re[k][i] = de->hist_re[k][i - 1];
            de->hist_im[k][i] = de->hist_im[k][i - 1];
        }
        de->hist_re[k][0] = yv_re[0];
        de->hist_im[k][0] = yv_im[0];
    }
}

/* ---- one full frame: analyze -> enhance -> synthesize -------------------- */

static void df_run_frame(df_enhance_t *de)
{
    int n, k;
    short *emit = de->out_buf + de->out_fill;

    /* slide the analysis buffer and append the new hop */
    memmove(de->ana, de->ana + de->hop, (de->W - de->hop) * sizeof(float));
    for(n = 0; n < de->hop; n++)
        de->ana[de->W - de->hop + n] = (float)de->in_buf[n];

    /* windowed, zero-padded FFT */
    for(n = 0; n < de->W; n++) de->fft_re[n] = de->ana[n] * de->win[n];
    for(n = de->W; n < de->N; n++) de->fft_re[n] = 0.0f;
    memset(de->fft_im, 0, de->N * sizeof(float));
    df_fft(de, de->fft_re, de->fft_im, 0);
    for(k = 0; k < de->K; k++) {
        de->Xre[k] = de->fft_re[k];
        de->Xim[k] = de->fft_im[k];
    }

    if(de->bypass) {
        memcpy(de->X1re, de->Xre, de->K * sizeof(float));
        memcpy(de->X1im, de->Xim, de->K * sizeof(float));
    }
    else {
        df_stage1(de);                 /* also computes pp_bin for stage 2 */
        if(de->stage2_on && de->df_alpha_max > 0.0f)
            df_stage2(de);
    }

    /* rebuild the conjugate-symmetric spectrum; DC/Nyquist forced real */
    de->fft_re[0] = de->X1re[0];
    de->fft_im[0] = 0.0f;
    de->fft_re[de->N / 2] = de->X1re[de->K - 1];
    de->fft_im[de->N / 2] = 0.0f;
    for(k = 1; k < de->N / 2; k++) {
        de->fft_re[k] = de->X1re[k];
        de->fft_im[k] = de->X1im[k];
        de->fft_re[de->N - k] =  de->X1re[k];
        de->fft_im[de->N - k] = -de->X1im[k];
    }
    df_fft(de, de->fft_re, de->fft_im, 1);

    /* synthesis window over the true window span; the zero-pad tail carries
     * the filter spillover and is overlap-added unwindowed (it is exactly
     * zero at unity gain, preserving perfect reconstruction). */
    for(n = 0; n < de->W; n++)
        de->ola[n] += de->fft_re[n] * de->win[n];
    for(n = de->W; n < de->N; n++)
        de->ola[n] += de->fft_re[n];

    /* emit one hop */
    for(n = 0; n < de->hop; n++) {
        float v = de->ola[n];
        long s = lround((double)v);
        if(s > 32767)  s = 32767;
        if(s < -32768) s = -32768;
        emit[n] = (short)s;
    }
    de->out_fill += de->hop;
    memmove(de->ola, de->ola + de->hop, (de->N - de->hop) * sizeof(float));
    memset(de->ola + de->N - de->hop, 0, de->hop * sizeof(float));

    de->frame_counter++;
}

/* ---- public processing --------------------------------------------------- */

void df_enhance_process(df_enhance_t *de, short *samples, size_t count,
                        int is_speech)
{
    size_t done = 0;
    if(!de || !samples || !count) return;
    if(!de->rate) return;                     /* not initialized: passthrough */
    de->hint = is_speech ? 1 : 0;

    while(done < count) {
        size_t take = (size_t)(de->hop - de->in_fill);
        if(take > count - done) take = count - done;

        memcpy(de->in_buf + de->in_fill, samples + done, take * sizeof(short));
        de->in_fill += (int)take;
        if(de->in_fill == de->hop) {
            df_run_frame(de);
            de->in_fill = 0;
        }

        /* pop the same number of samples back into the caller's buffer;
         * the pre-seeded hop guarantees availability (out_fill+in_fill==hop
         * invariant between calls). */
        memcpy(samples + done, de->out_buf, take * sizeof(short));
        de->out_fill -= (int)take;
        memmove(de->out_buf, de->out_buf + take,
                (size_t)de->out_fill * sizeof(short));

        done += take;
    }
}
