/*
 * rn_denoise.c - RNNoise wrapper with 8/16 kHz <-> 48 kHz polyphase
 * resampling. See rn_denoise.h for the overview and the copy-sync note.
 */
#include "rn_denoise.h"
#include "rnnoise.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

#define RN_FRAME48   480             /* RNNoise frame at 48 kHz (10 ms)      */
#define RN_MAX_L     6               /* 48000/8000                           */
#define RN_MAX_HOP   480             /* 10 ms at 48 kHz                      */
#define RN_SUBTAPS   48              /* FIR taps per polyphase branch: the
                                      * narrow telephony cutoff (3.7k of 24k
                                      * Nyquist) needs a long prototype for a
                                      * usable transition band (~0.9 kHz at
                                      * 288 taps; 96 taps blurred the whole
                                      * 2-5 kHz range)                       */
#define RN_MAX_TAPS  (RN_SUBTAPS * RN_MAX_L + 1) /* odd prototype (289): the
                                      * total interp+decim group delay
                                      * (taps-1) lands on a whole sample at
                                      * the low rate                        */

struct rn_denoise_t {
    DenoiseState *st;                /* vendored RNNoise (little model)      */

    unsigned rate;                   /* 0 until init                         */
    int L;                           /* 48000 / rate                         */
    int hop;                         /* rate / 100 (10 ms)                   */
    int taps;                        /* actual prototype length (RN_SUBTAPS*L)*/
    float fir[RN_MAX_TAPS];          /* lowpass prototype, cutoff 0.9*rate/2 */

    /* I/O rings at the configured rate (df_enhance pattern: out ring is
     * pre-seeded with one hop of zeros, so out_fill + in_fill == hop between
     * calls and arbitrary chunkings are bit-identical). */
    short in_buf[RN_MAX_HOP];
    int   in_fill;
    short out_buf[2 * RN_MAX_HOP];
    int   out_fill;

    /* resampler FIR histories */
    float up_hist[RN_SUBTAPS + 1];   /* last input samples (rate domain)     */
    float dn_hist[RN_MAX_TAPS + RN_FRAME48]; /* 48 kHz stream for decimation */

    /* dry path, delayed by the internal (non-ring) pipeline delay so the
     * wet/dry mix is phase-aligned. RNNoise itself delays by 2*FRAME (960
     * samples at 48 kHz -- measured, not the 1*FRAME the synthesis code
     * suggests) plus the two FIR group delays. */
    float wet;                       /* 0..1, default 0.8                    */
    int   dry_delay;                 /* = 2*RN_FRAME48/L + (taps-1)/L        */
    short dry_line[3 * RN_MAX_HOP];  /* dry_delay + hop <= 1440              */

    double vad_prob;
};

/* ---- lifecycle ----------------------------------------------------------- */

rn_denoise_t *rn_denoise_create(void)
{
    rn_denoise_t *rn = (rn_denoise_t *)calloc(1, sizeof(*rn));
    if(!rn) return NULL;
    rn->st = rnnoise_create(NULL);   /* built-in (vendored little) model */
    if(!rn->st) { free(rn); return NULL; }
    rn->wet = 0.8f;
    return rn;
}

void rn_denoise_destroy(rn_denoise_t *rn)
{
    if(!rn) return;
    if(rn->st) rnnoise_destroy(rn->st);
    free(rn);
}

void rn_denoise_reset(rn_denoise_t *rn)
{
    if(!rn) return;
    rn->in_fill = 0;
    memset(rn->in_buf, 0, sizeof(rn->in_buf));
    memset(rn->out_buf, 0, sizeof(rn->out_buf));
    rn->out_fill = rn->hop;          /* one-hop pre-seed, see struct comment */
    memset(rn->up_hist, 0, sizeof(rn->up_hist));
    memset(rn->dn_hist, 0, sizeof(rn->dn_hist));
    memset(rn->dry_line, 0, sizeof(rn->dry_line));
    rn->vad_prob = 0.0;
    if(rn->st) rnnoise_init(rn->st, NULL);   /* clear GRU/conv/FFT state */
}

int rn_denoise_init(rn_denoise_t *rn, unsigned sample_rate)
{
    int n;
    if(!rn) return -1;
    if(sample_rate < 8000 || sample_rate > 48000 ||
       48000 % sample_rate != 0 || sample_rate % 100 != 0)
        return -1;

    rn->rate = sample_rate;
    rn->L    = (int)(48000 / sample_rate);
    rn->hop  = (int)(sample_rate / 100);
    rn->taps = RN_SUBTAPS * rn->L + 1;
    rn->dry_delay = 2 * RN_FRAME48 / rn->L +
                    (rn->L > 1 ? (rn->taps - 1) / rn->L : 0);

    /* Windowed-sinc lowpass at 48 kHz, cutoff 0.92 * (rate/2): the shared
     * anti-imaging (interpolation) and anti-aliasing (decimation) prototype.
     * Blackman window; unity DC gain per output stream. */
    if(rn->L > 1) {
        double fc = 0.92 * (sample_rate / 2.0) / 48000.0;  /* cycles/sample */
        double sum = 0.0;
        int c = (rn->taps - 1) / 2;          /* taps is odd: integer center */
        for(n = 0; n < rn->taps; n++) {
            double t = n - c;
            double s = t == 0.0 ? 2.0 * fc
                                : sin(2.0 * M_PI * fc * t) / (M_PI * t);
            double w = 0.42 - 0.5 * cos(2.0 * M_PI * n / (rn->taps - 1))
                            + 0.08 * cos(4.0 * M_PI * n / (rn->taps - 1));
            rn->fir[n] = (float)(s * w);
            sum += s * w;
        }
        for(n = 0; n < rn->taps; n++)
            rn->fir[n] = (float)(rn->fir[n] / sum);        /* DC gain 1 */
    }

    rn_denoise_reset(rn);
    return 0;
}

void rn_denoise_wet_set(rn_denoise_t *rn, double wet)
{
    if(!rn) return;
    if(wet < 0.0) wet = 0.0;
    if(wet > 1.0) wet = 1.0;
    rn->wet = (float)wet;
}

/* ---- introspection ------------------------------------------------------- */

double rn_denoise_vad_prob(const rn_denoise_t *rn)
{ return rn ? rn->vad_prob : 0.0; }

unsigned rn_denoise_latency_samples(const rn_denoise_t *rn)
{
    if(!rn || !rn->rate) return 0;
    /* ring hop + RNNoise's 2-frame delay + both FIR group delays */
    return (unsigned)(rn->hop + rn->dry_delay);
}

/* ---- one 10 ms hop: upsample -> rnnoise@48k -> decimate ------------------ */

static void rn_run_frame(rn_denoise_t *rn)
{
    float buf48[RN_FRAME48];
    short *emit = rn->out_buf + rn->out_fill;
    const float dry_g = 1.0f - rn->wet;
    int n, p, k;

    /* slide the dry delay line and append the fresh hop; dry_line[0..hop)
     * is then aligned with this frame's wet output */
    memmove(rn->dry_line, rn->dry_line + rn->hop,
            (size_t)rn->dry_delay * sizeof(short));
    memcpy(rn->dry_line + rn->dry_delay, rn->in_buf,
           (size_t)rn->hop * sizeof(short));

    if(rn->L == 1) {
        for(n = 0; n < RN_FRAME48; n++) buf48[n] = (float)rn->in_buf[n];
        rn->vad_prob = rnnoise_process_frame(rn->st, buf48, buf48);
        for(n = 0; n < rn->hop; n++) {
            float v = rn->wet * buf48[n] + dry_g * (float)rn->dry_line[n];
            long s = lround((double)v);
            if(s > 32767)  s = 32767;
            if(s < -32768) s = -32768;
            emit[n] = (short)s;
        }
        rn->out_fill += rn->hop;
        return;
    }

    /* polyphase interpolation: for input sample n, phase p output is
     * L * sum_k fir[k*L + p] * x[n - k] (gain L restores the amplitude
     * lost to zero-stuffing) */
    for(n = 0; n < rn->hop; n++) {
        /* slide the (rate-domain) history: newest first */
        memmove(rn->up_hist + 1, rn->up_hist,
                RN_SUBTAPS * sizeof(float));
        rn->up_hist[0] = (float)rn->in_buf[n];
        for(p = 0; p < rn->L; p++) {
            float acc = 0.0f;
            for(k = 0; k * rn->L + p < rn->taps; k++)
                acc += rn->fir[k * rn->L + p] * rn->up_hist[k];
            buf48[n * rn->L + p] = (float)rn->L * acc;
        }
    }

    rn->vad_prob = rnnoise_process_frame(rn->st, buf48, buf48);

    /* append to the 48 kHz decimation history, then take every L-th sample
     * through the same lowpass */
    memmove(rn->dn_hist, rn->dn_hist + RN_FRAME48,
            (rn->taps) * sizeof(float));
    memcpy(rn->dn_hist + rn->taps, buf48, RN_FRAME48 * sizeof(float));
    for(n = 0; n < rn->hop; n++) {
        float acc = 0.0f;
        const float *x = rn->dn_hist + rn->taps + n * rn->L;
        for(k = 0; k < rn->taps; k++)
            acc += rn->fir[k] * x[-k];
        acc = rn->wet * acc + dry_g * (float)rn->dry_line[n];
        {
            long s = lround((double)acc);
            if(s > 32767)  s = 32767;
            if(s < -32768) s = -32768;
            emit[n] = (short)s;
        }
    }
    rn->out_fill += rn->hop;
}

/* ---- public processing --------------------------------------------------- */

void rn_denoise_process(rn_denoise_t *rn, short *samples, size_t count,
                        int is_speech)
{
    size_t done = 0;
    (void)is_speech;                 /* API symmetry with df_enhance */
    if(!rn || !samples || !count) return;
    if(!rn->rate) return;            /* not initialized: passthrough */

    while(done < count) {
        size_t take = (size_t)(rn->hop - rn->in_fill);
        if(take > count - done) take = count - done;

        memcpy(rn->in_buf + rn->in_fill, samples + done, take * sizeof(short));
        rn->in_fill += (int)take;
        if(rn->in_fill == rn->hop) {
            rn_run_frame(rn);
            rn->in_fill = 0;
        }

        memcpy(samples + done, rn->out_buf, take * sizeof(short));
        rn->out_fill -= (int)take;
        memmove(rn->out_buf, rn->out_buf + take,
                (size_t)rn->out_fill * sizeof(short));

        done += take;
    }
}
