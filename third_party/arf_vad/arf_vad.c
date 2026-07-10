/*
 * arf_vad.c - Improved energy/SNR based Voice Activity Detector.
 * See arf_vad.h for the rationale, the public contract and the list of
 * mechanical modifications made in this standalone extraction.
 */

#include <stdlib.h>
#include <math.h>

#include "arf_vad.h"

/* apt_log/arf_plugin_log are unavailable outside the UniMRCP plugin: every
 * original log call site is preserved, routed to this no-op sink, so the file
 * stays diffable against the plugin source (and the diagnostics can be
 * re-enabled by pointing the macro at a real printf-style logger). */
static void arf_vad_log_sink(const char *fmt, ...) { (void)fmt; }
#define ARF_VAD_LOG arf_vad_log_sink

/* Emit a debug line every Nth processed frame (25 * 10 ms = 250 ms). */
#define ARF_VAD_LOG_EVERY  25

/* ----------------------------------------------------------------------
 * Tunables that rarely need to be exposed.
 * -------------------------------------------------------------------- */

/* Asymmetric one-pole coefficients for the noise-floor tracker (per frame).
 * The floor falls quickly toward a new (lower) ambient level and rises slowly,
 * so a brief noise burst can never inflate it for long, and residual speech
 * leaking through cannot drag it up. Adaptation is frozen during speech.
 *
 * NOTE: the fall MUST stay fast. Slowing it (tried 0.05) leaves the floor
 * "stuck high" after the seed/TTS tail, so by the time the caller speaks the
 * SNR never clears the onset gate and normal speech stops triggering at all.
 * Keep 0.20. Wind false-triggers are handled by the libfvad speech gate in the
 * engine, not by detuning this tracker. */
#define NF_RISE_COEF   0.02   /* power > floor : slow rise   (~0.5 s @10ms)  */
#define NF_FALL_COEF   0.20   /* power < floor : fast fall   (~50 ms @10ms)  */

/* First-order DC blocker pole. y[n] = x[n]-x[n-1] + R*y[n-1]. Removes the DC
 * offset common on telephony lines so energy and ZCR are meaningful. */
#define DC_BLOCK_R     0.995

/* Zero-crossing-rate assist: in the "gray zone" between offset and onset SNR,
 * a frame with a high ZCR (fricative-like) is still treated as an onset. */
#define ZCR_FRICATIVE_MIN  0.20   /* fraction of sign changes per sample     */
#define ZCR_SNR_RELAX_DB   4.0    /* how far below onset the assist reaches  */

/* Floor for log power so digital silence does not yield -inf dB. */
#define POWER_EPS      1.0

/* Upper bound on the value the noise floor may be SEEDED with on the very first
 * frame. The stock detector seeded the floor from frame 0 unconditionally; when
 * a recording (or a barge-in capture) begins mid-word -- no leading silence --
 * that latches the floor onto SPEECH, so the opening word reads as SNR~0 and the
 * onset is suppressed until the floor slowly decays. The whole first word is then
 * lost ("bastaki kelime eksik") or mangled ("basi bozuk"). Seeding no higher than
 * this keeps a speech-loud first frame from poisoning the floor: a real opening
 * word (40-60 dB power) then clears onset_snr immediately, while a genuinely quiet
 * start still seeds from its own (lower) level. The floor rises toward the true
 * ambient on the first non-speech frames; abs_silence_level + the spectral veto
 * remain the guards against a non-silent start false-onsetting. ~30 dB sits below
 * speech and at/above a clean telephony line's idle tone. */
#define FLOOR_SEED_MAX_DB  30.0

/* Absolute mean-abs amplitude below which a frame is unconditionally silence.
 * Acts as a safety net against the adaptive floor latching the detector in the
 * "speech" state: if the floor was trained on artificially-low audio (comfort
 * noise / half-duplex silence during TTS) and the live mic then engages with a
 * higher ambient, every "silence" frame would otherwise read as SNR>offset and
 * the trailing-silence (end-of-speech) decision would never fire. Mirrors the
 * stock mpf_activity_detector's absolute level threshold. 0 disables it. */
#define ABS_SILENCE_DEFAULT  120

/* Proximity gate (dominant-talker) defaults: OFF. The near talker on a handset
 * is much louder than background room speech, but the right thresholds depend on
 * the line, so they are calibrated per deployment (and on a real babble dump)
 * rather than guessed here. 0 = disabled => behaviour identical to before. */
#define ONSET_LEVEL_DEFAULT     0
#define DOMINANT_DROP_DEFAULT   0.0

/* Consecutive qualifying frames a strict onset gate (absolute floor / adaptive
 * margin) must see before it lets the onset through. 0 => single-frame (legacy);
 * a small run (e.g. 3 = 30 ms) rejects lone loud transients, mpf-style. */
#define ONSET_CONFIRM_DEFAULT   0

/* How fast the tracked near-talker reference follows the speech level: it snaps
 * up to any louder speech frame (so the loudest near burst sets the bar) and
 * decays very slowly, so the bar survives short pauses but still adapts down over
 * a long call / if the talker moves off the mouthpiece. ~0.001 @10ms ~ 10 s. */
#define SPEECH_REF_DECAY        0.001

/* The near-talker reference (speech_ref) snaps to the LOUDEST confirmed frame and
 * decays only glacially (SPEECH_REF_DECAY), so a single loud burst pins it high
 * for tens of seconds. Persisting it across utterances is intentional (a quiet
 * follow-up by the same talker is judged against their own level), but a STALE
 * loud peak must not outlive the talker: a normal-level talker arriving later
 * then sits >PROX_ASSIGN_BAND_DB below it, gets mis-assigned to the far/background
 * cluster, and raises the adaptive onset bar against itself. So mirror the far
 * cluster: release speech_ref after SPEECH_LEAVE_MS of no confirmed near speech,
 * which re-arms the PHASE-1 absolute onset gate for the next fresh talker. */
#define SPEECH_LEAVE_MS         4000   /* near reference released after this idle */

/* Adaptive proximity (two-cluster) gate. A confirmed-speech frame sitting more
 * than PROX_ASSIGN_BAND_DB below the near talker is treated as the background
 * ("far") cluster rather than a dip of the near talker. The far level follows a
 * slow EWMA (FAR_TRACK_COEF) so a single frame cannot swing it, and the cluster
 * EXPIRES after FAR_LEAVE_MS without any background-level frame, so a background
 * talker that LEAVES stops gating later (quieter) near speech. */
#define PROX_ASSIGN_BAND_DB     6.0
#define FAR_TRACK_COEF          0.05   /* ~200 ms EWMA toward the background level   */
#define FAR_LEAVE_MS            4000   /* background considered gone after this idle */

/** Internal state machine (kept identical in spirit to mpf_activity_detector) */
typedef enum {
    ARF_STATE_IDLE,          /**< confirmed silence                       */
    ARF_STATE_ONSET,         /**< speech-candidate, accumulating duration */
    ARF_STATE_ACTIVE,        /**< confirmed speech                        */
    ARF_STATE_OFFSET         /**< silence-candidate, accumulating duration*/
} arf_state_e;

struct arf_vad_t {
    /* timing (ms) */
    size_t       frame_dur;
    size_t       speech_timeout;
    size_t       silence_timeout;
    size_t       noinput_timeout;

    /* SNR thresholds (dB) */
    double       onset_snr;
    double       offset_snr;
    int          use_zcr;

    /* Spectral speech/noise vote for the current frame (set by the caller before
     * each arf_vad_process): 1 = speech, 0 = noise (veto -> silence), -1 = none.
     * Consumed and reset to -1 every frame. See arf_vad_spectral_vote_set. */
    int          spec_vote;

    /* High-confidence bypass of the spectral (libfvad) veto, in dB of SNR. A
     * noise vote (spec_vote==0) is IGNORED for any frame whose snr >= this -- such
     * a frame is unambiguous voiced near-field speech that libfvad's slow 250 ms
     * window mislabels on muffled telephony (a short loud word like "evet"), and
     * honouring its veto would both block the onset and chop the ONSET integrator
     * on the word's quieter dips. The absolute onset_level gate still guards the
     * ONSET decision separately; the speech_timeout integrator + proximity gates
     * still guard the ACTIVITY event. 0 disables => veto always honoured (default,
     * behaviour byte-identical). See arf_vad_spec_bypass_snr_set. */
    double       spec_bypass_snr;

    /* Absolute mean-abs amplitude floor below which a frame is always silence
     * (anti-latch safety net; 0 disables). See ABS_SILENCE_DEFAULT. */
    size_t       abs_silence_level;

    /* Proximity (dominant-talker) gate. onset_level: absolute mean-abs a frame
     * must reach to START speech (0 disables). dominant_drop_db: dB below the
     * tracked near-talker level at which a frame can no longer start a new
     * segment (0 disables). Both onset-only. See arf_vad.h. */
    size_t       onset_level;
    size_t       onset_confirm_frames; /* consecutive strict-gate frames to onset */
    size_t       strict_run;           /* runtime: consecutive qualifying frames  */
    double       dominant_drop_db;
    double       speech_ref_db;   /* tracked near-talker level (dB)        */
    int          speech_ref_set;  /* 0 until first speech confirms a level */
    size_t       speech_idle_ms;  /* ms since the near reference last got fed */

    /* Adaptive proximity: a SECOND (background) cluster + its config/expiry. The
     * onset gate blocks a fresh segment that starts within adaptive_margin_db of
     * this learned background level. far_idle_ms ages the cluster out when the
     * background falls quiet. adaptive_margin_db 0 disables. See arf_vad.h. */
    double       adaptive_margin_db;
    double       far_ref_db;      /* tracked background level (dB)         */
    int          far_ref_set;     /* 0 until a background cluster is known  */
    size_t       far_idle_ms;     /* ms since the far cluster last got fed  */

    /* runtime state */
    arf_state_e  state;
    size_t       duration;       /* ms spent accumulating in the current state */
    size_t       idle_duration;  /* ms of silence since (re)start, for noinput  */
    int          nf_initialized;
    double       noise_floor_db;
    double       last_level_db;

    /* DC blocker history (persists across frames) */
    double       dc_x_prev;
    double       dc_y_prev;

    /* debug log throttle: frames processed since reset */
    size_t       log_frames;
    /* debug log throttle: consecutive frames an at-level onset stayed blocked */
    size_t       onset_block_log;
    /* debug log throttle: consecutive frames the spectral veto was bypassed */
    size_t       spec_bypass_log;
};

/* ---- lifecycle ------------------------------------------------------- */

arf_vad_t* arf_vad_create(void)
{
    arf_vad_t *vad = malloc(sizeof(*vad));
    if(!vad) {
        return NULL;
    }
    vad->frame_dur       = ARF_VAD_FRAME_TIME_BASE; /* 10 ms */
    vad->speech_timeout  = 300;
    vad->silence_timeout = 300;
    vad->noinput_timeout = 5000;
    vad->onset_snr       = 9.0;
    vad->offset_snr      = 5.0;
    vad->use_zcr         = 1;
    vad->abs_silence_level = ABS_SILENCE_DEFAULT;
    vad->onset_level       = ONSET_LEVEL_DEFAULT;
    vad->onset_confirm_frames = ONSET_CONFIRM_DEFAULT;
    vad->dominant_drop_db  = DOMINANT_DROP_DEFAULT;
    vad->adaptive_margin_db = 0.0;   /* adaptive proximity OFF by default */
    vad->spec_bypass_snr = 0.0;      /* spectral-veto bypass OFF by default */
    arf_vad_reset(vad);
    return vad;
}

void arf_vad_destroy(arf_vad_t *vad)
{
    free(vad);
}

void arf_vad_reset_utterance(arf_vad_t *vad)
{
    vad->state          = ARF_STATE_IDLE;
    vad->duration       = 0;
    vad->idle_duration  = 0;
    vad->nf_initialized = 0;
    vad->noise_floor_db = 0.0;
    vad->last_level_db  = 0.0;
    vad->dc_x_prev      = 0.0;
    vad->dc_y_prev      = 0.0;
    vad->spec_vote      = -1;
    vad->strict_run     = 0;
    vad->log_frames     = 0;
    vad->onset_block_log = 0;
    vad->spec_bypass_log = 0;
}


void arf_vad_reset(arf_vad_t *vad)
{
    arf_vad_reset_utterance(vad);
    vad->speech_ref_db  = 0.0;
    vad->speech_ref_set = 0;
    vad->speech_idle_ms = 0;
    vad->far_ref_db     = 0.0;
    vad->far_ref_set    = 0;
    vad->far_idle_ms    = 0;
}

/* ---- setters --------------------------------------------------------- */

void arf_vad_speech_timeout_set(arf_vad_t *vad, size_t ms)  { vad->speech_timeout  = ms; }
void arf_vad_silence_timeout_set(arf_vad_t *vad, size_t ms) { vad->silence_timeout = ms; }
void arf_vad_noinput_timeout_set(arf_vad_t *vad, size_t ms) { vad->noinput_timeout = ms; }
void arf_vad_frame_duration_set(arf_vad_t *vad, size_t ms)  { if(ms) vad->frame_dur = ms; }
void arf_vad_onset_snr_set(arf_vad_t *vad, double db)           { vad->onset_snr  = db; }
void arf_vad_offset_snr_set(arf_vad_t *vad, double db)          { vad->offset_snr = db; }
void arf_vad_zcr_enable(arf_vad_t *vad, int enable)             { vad->use_zcr = enable ? 1 : 0; }
void arf_vad_abs_silence_level_set(arf_vad_t *vad, size_t level) { vad->abs_silence_level = level; }
void arf_vad_onset_level_set(arf_vad_t *vad, size_t level)  { vad->onset_level = level; }
void arf_vad_onset_confirm_frames_set(arf_vad_t *vad, size_t frames) { vad->onset_confirm_frames = frames; }
void arf_vad_spec_bypass_snr_set(arf_vad_t *vad, double db)     { vad->spec_bypass_snr = db; }
void arf_vad_dominant_drop_set(arf_vad_t *vad, double db)       { vad->dominant_drop_db = (db > 0.0) ? db : 0.0; }
void arf_vad_adaptive_proximity_set(arf_vad_t *vad, double db)  { vad->adaptive_margin_db = (db > 0.0) ? db : 0.0; }
void arf_vad_spectral_vote_set(arf_vad_t *vad, int vote)        { vad->spec_vote = vote; }

double arf_vad_last_level_db(const arf_vad_t *vad)  { return vad->last_level_db; }
double arf_vad_noise_floor_db(const arf_vad_t *vad) { return vad->noise_floor_db; }
double arf_vad_speech_ref_db(const arf_vad_t *vad)  { return vad->speech_ref_set ? vad->speech_ref_db : 0.0; }
double arf_vad_far_ref_db(const arf_vad_t *vad)     { return vad->far_ref_set ? vad->far_ref_db : 0.0; }

const char* arf_vad_state_str(const arf_vad_t *vad)
{
    switch(vad->state) {
        case ARF_STATE_IDLE:   return "IDLE";
        case ARF_STATE_ONSET:  return "ONSET";
        case ARF_STATE_ACTIVE: return "ACTIVE";
        case ARF_STATE_OFFSET: return "OFFSET";
    }
    return "?";
}

size_t arf_vad_state_duration_ms(const arf_vad_t *vad) { return vad->duration; }

/* ---- feature extraction --------------------------------------------- */

/* Compute frame power (dB), zero-crossing rate (after DC removal) and the raw
 * mean-abs amplitude. Returns power in dB; writes the ZCR (0..1) to *out_zcr and
 * the mean-abs level (0..32767, same metric as the stock detector) to
 * *out_level. */
static double arf_vad_features(arf_vad_t *vad, const int16_t *cur, size_t count,
                               double *out_zcr, size_t *out_level)
{
    double sumsq = 0.0;
    size_t sum_abs = 0;
    size_t zc = 0;
    int prev_sign = 0;
    double x_prev = vad->dc_x_prev;
    double y_prev = vad->dc_y_prev;
    size_t i;

    if(!cur || count == 0) {
        *out_zcr = 0.0;
        *out_level = 0;
        return 0.0;
    }

    for(i = 0; i < count; i++) {
        double x = (double)cur[i];
        /* DC blocker: high-pass that removes the constant offset */
        double y = x - x_prev + DC_BLOCK_R * y_prev;
        int sign = (y > 0.0) ? 1 : (y < 0.0 ? -1 : 0);

        x_prev = x;
        y_prev = y;

        sumsq += y * y;
        sum_abs += (cur[i] < 0) ? (size_t)(-cur[i]) : (size_t)cur[i];
        if(sign != 0) {
            if(prev_sign != 0 && sign != prev_sign) {
                zc++;
            }
            prev_sign = sign;
        }
    }

    /* persist filter history for the next frame */
    vad->dc_x_prev = x_prev;
    vad->dc_y_prev = y_prev;

    *out_zcr = (count > 1) ? ((double)zc / (double)(count - 1)) : 0.0;
    *out_level = sum_abs / count;
    return 10.0 * log10(sumsq / (double)count + POWER_EPS);
}

/* Track the background noise floor from non-speech frames only. */
static void arf_vad_update_noise_floor(arf_vad_t *vad, double power_db)
{
    if(!vad->nf_initialized) {
        vad->noise_floor_db = power_db;
        vad->nf_initialized = 1;
        return;
    }
    if(power_db < vad->noise_floor_db) {
        vad->noise_floor_db += NF_FALL_COEF * (power_db - vad->noise_floor_db);
    }
    else {
        vad->noise_floor_db += NF_RISE_COEF * (power_db - vad->noise_floor_db);
    }
}

/* ---- main entry ------------------------------------------------------ */

arf_vad_event_e arf_vad_process(arf_vad_t *vad, const int16_t *samples, size_t count)
{
    arf_vad_event_e det_event = ARF_VAD_EVENT_NONE;
    double zcr = 0.0;
    size_t level = 0;   /* raw mean-abs amplitude of this frame */
    double power_db;
    double snr;
    int above_offset;   /* loud enough to be (still) speech    */
    int above_onset;    /* loud enough to *start* speech        */
    int energy_onset = 0; /* did SNR/ZCR alone want an onset (pre-veto)?       */
    int spec_was = -1;    /* libfvad vote this frame, captured for diagnostics */

    if(samples && count) {
        power_db = arf_vad_features(vad, samples, count, &zcr, &level);
        vad->last_level_db = power_db;
        if(!vad->nf_initialized) {
            /* Seed the floor from the first frame but never ABOVE FLOOR_SEED_MAX_DB,
             * so a recording that starts mid-speech (no leading silence) cannot
             * latch the floor onto speech and swallow the opening word. A quiet
             * start still seeds from its own lower level. Classify as silence. */
            vad->noise_floor_db = (power_db < FLOOR_SEED_MAX_DB) ? power_db : FLOOR_SEED_MAX_DB;
            vad->nf_initialized = 1;
            return ARF_VAD_EVENT_NONE;
        }
    }
    else {
        /* No audio payload (e.g. CN/DTX): treat as a silence frame so the
         * no-input and trailing-silence timers keep advancing. */
        if(!vad->nf_initialized) {
            return ARF_VAD_EVENT_NONE;
        }
        power_db = vad->noise_floor_db; /* SNR 0 -> silence */
        level = 0;
    }

    snr = power_db - vad->noise_floor_db;
    above_offset = (snr >= vad->offset_snr);
    above_onset  = (snr >= vad->onset_snr);

    /* ZCR assist: a fricative-like frame in the gray zone counts as an onset. */
    if(vad->use_zcr && !above_onset &&
       snr >= (vad->onset_snr - ZCR_SNR_RELAX_DB) && zcr >= ZCR_FRICATIVE_MIN) {
        above_onset = 1;
    }

    /* Snapshot the pure energy/SNR onset decision BEFORE any veto, so the
     * diagnostic below can tell "energy wanted to start but a gate blocked it"
     * apart from "energy itself never reached the onset SNR". */
    energy_onset = above_onset;

    /* Absolute silence override: a frame quieter than abs_silence_level is
     * silence no matter what the adaptive SNR says. This is what guarantees an
     * end-of-speech (trailing-silence) decision can always be reached, even if
     * the noise floor was trained too low (comfort-noise/half-duplex silence
     * during TTS) and the live mic's real ambient now reads as SNR>offset. It
     * also lets the floor resume tracking, since the frame now counts as
     * non-speech for the adaptation gate below. */
    if(vad->abs_silence_level && level < vad->abs_silence_level) {
        above_offset = 0;
        above_onset  = 0;
    }

    /* Spectral veto: a trained sub-band classifier (libfvad) says THIS frame is
     * noise, not speech -> force it to count as silence regardless of energy/SNR.
     * This is what stops wind/clicks/room-noise -- before speech OR inside a
     * pause -- from starting or holding the utterance. A speech vote (1) or
     * "unknown" (-1) leaves the energy decision above untouched. Because the
     * frame now reads as non-speech, it also feeds the noise-floor adaptation
     * below, so the floor keeps tracking the real ambient. */
    spec_was = vad->spec_vote;   /* capture before consuming, for diagnostics */
    if(vad->spec_vote == 0) {
        /* High-confidence bypass: a frame at snr >= spec_bypass_snr is unambiguous
         * voiced near-field speech (tens of dB over ambient). libfvad exists to
         * disambiguate MARGINAL energy (a gust/babble burst that merely crosses
         * onset_snr); on muffled 8 kHz telephony it under-votes speech badly (0-7 of
         * 25 frames even on a loud "evet"), so its veto both BLOCKS the onset and, on
         * the word's quieter inter-phoneme dips, clears above_offset and DRAINS the
         * ONSET integrator -- a short word never confirms. Honour energy over libfvad
         * here. We deliberately do NOT also require level >= onset_level: that
         * absolute gate is enforced separately for the ONSET decision (strict gate
         * below), and demanding it HERE would re-veto the sub-onset_level dips inside
         * a word and chop the accrual. Lone transients are still rejected downstream
         * (speech_timeout integrator + proximity). spec_bypass_snr == 0 => never
         * bypass (default, behaviour unchanged). */
        if(vad->spec_bypass_snr > 0.0 && snr >= vad->spec_bypass_snr) {
            if(vad->spec_bypass_log % ARF_VAD_LOG_EVERY == 0) {
                ARF_VAD_LOG("arf_vad: SPEC VETO BYPASSED @level=%zu pwr=%.1f "
                    "snr=%.1f(>=%.1f) -- loud short-word onset kept despite libfvad=noise",
                    level, power_db, snr, vad->spec_bypass_snr);
            }
            vad->spec_bypass_log++;
        }
        else {
            vad->spec_bypass_log = 0;
            above_offset = 0;
            above_onset  = 0;
        }
    }
    else {
        vad->spec_bypass_log = 0;
    }
    vad->spec_vote = -1;   /* consume: default to energy-only for the next frame */

    /* ---- Proximity gates (all ONSET-ONLY: they stop a far/quiet talker from
     * STARTING a segment but never clear above_offset, so the near talker's own
     * quiet tail stays governed by offset_snr/abs_silence and is never chopped). */

    /* Strict onset gate with N-consecutive-frame confirmation, SELF-DISARMING
     * across two phases. A single "crossed the level -> hop" frame must NOT open
     * a segment (mpf_activity_detector requires the level to hold for the WHOLE
     * speech_timeout; here a shorter run of onset_confirm_frames is enough), so a
     * lone loud transient cannot start speech. The strict condition is phase-
     * dependent:
     *   - PHASE 1 (session/call start, no near-talker reference yet): the
     *     absolute floor level >= onset_level. Background babble (far/quiet)
     *     speaking BEFORE the caller cannot clear it, so it can no longer steal
     *     the unguarded first onset -- the one window the relative gates cannot
     *     cover (they need a reference that does not exist yet).
     *   - PHASE 2 (after the near talker sets the reference): hand off to the
     *     adaptive proximity gate -- power_db >= far_ref_db + adaptive_margin_db,
     *     i.e. a fresh segment must START at least margin dB ABOVE the learned
     *     background. The boundary is anchored to the BACKGROUND we reject, not
     *     the near talker, so a loud transient that inflated the near reference
     *     cannot raise the bar and choke the genuine near talker.
     * Both are ONSET-ONLY (never touch above_offset), and PHASE 1 RELEASES the
     * instant speech_ref_set turns true -- so it is not a permanent absolute bar:
     * a near talker who later speaks quietly is judged relative to their OWN
     * level, never blocked here (no "bastaki kelime eksik" regression). Any frame
     * failing the strict condition resets the run; onset_confirm_frames == 0
     * collapses to the single-frame (legacy) behaviour. */
    {
        int strict_active = 0, strict_ok = 1;
        if(!vad->speech_ref_set) {
            if(vad->onset_level) {
                strict_active = 1;
                if(level < vad->onset_level) strict_ok = 0;
            }
        }
        else if(vad->adaptive_margin_db > 0.0 && vad->far_ref_set) {
            strict_active = 1;
            if(power_db < vad->far_ref_db + vad->adaptive_margin_db) strict_ok = 0;
        }
        if(strict_active) {
            size_t need = vad->onset_confirm_frames ? vad->onset_confirm_frames : 1;
            vad->strict_run = strict_ok ? (vad->strict_run + 1) : 0;
            if(vad->strict_run < need) above_onset = 0;
        }
        else {
            vad->strict_run = 0;
        }
    }

    /* Relative dominant-talker gate (separate lever; OFF by default here -- the
     * adaptive proximity above is the post-reference gate in this deployment). */
    if(vad->dominant_drop_db > 0.0 && vad->speech_ref_set &&
       power_db < vad->speech_ref_db - vad->dominant_drop_db) {
        above_onset = 0;
    }

    /* ---- WHY-BLOCKED diagnostic ----------------------------------------
     * The hard case to debug is "I reached the level on the speakerphone but
     * the VAD won't start, and the logs don't say why". Whenever a frame that
     * is at/above the configured onset_level still fails to onset while we are
     * idle/in a pause, emit ONE throttled line naming every quantity that can
     * hold it down: the SNR vs the onset SNR (floor crept up?), the libfvad
     * vote (spec_was==0 => vetoed as noise), the strict-run progress, and the
     * adaptive bar far_ref+margin the frame must clear in PHASE 2. Reads at a
     * glance e.g. "pwr=33.0 < bar=35.2" => adaptive gate; "snr=12<18" => floor;
     * "spec=0" => libfvad; "run=1/3" => confirmation not yet met. */
    if(vad->onset_level && level >= vad->onset_level && !above_onset &&
       (vad->state == ARF_STATE_IDLE || vad->state == ARF_STATE_OFFSET)) {
        if(vad->onset_block_log % ARF_VAD_LOG_EVERY == 0) {
            double bar = vad->far_ref_set ? vad->far_ref_db + vad->adaptive_margin_db : 0.0;
            ARF_VAD_LOG("arf_vad: ONSET BLOCKED @level=%zu pwr=%.1f floor=%.1f snr=%.1f(need>=%.1f) "
                "energy_onset=%d | phase=%s strict_run=%zu/%zu spec=%d | "
                "speech_ref=%.1f far_ref=%.1f%s adaptive_bar=%.1f(+%.1f)",
                level, power_db, vad->noise_floor_db, snr, vad->onset_snr,
                energy_onset,
                vad->speech_ref_set ? "ADAPTIVE" : "FLOOR",
                vad->strict_run,
                (size_t)(vad->onset_confirm_frames ? vad->onset_confirm_frames : 1),
                spec_was,
                vad->speech_ref_set ? vad->speech_ref_db : 0.0,
                vad->far_ref_set ? vad->far_ref_db : 0.0,
                vad->far_ref_set ? "" : "(unset)",
                bar, vad->adaptive_margin_db);
        }
        vad->onset_block_log++;
    }
    else {
        vad->onset_block_log = 0;
    }

    /* Track the near-talker reference from confirmed (sustained) speech: snap up
     * to any louder frame so the loudest near burst sets the bar, then decay
     * slowly so it survives pauses but still follows the talker over a long call.
     * Consumed by the dominant_drop gate above on subsequent onsets. Releasing it
     * after SPEECH_LEAVE_MS of no confirmed near speech keeps a stale loud peak
     * from outliving the talker and blocking a normal-level talker who arrives
     * later (the bug: that talker fell into the far cluster and raised its own
     * onset bar). The release re-arms the PHASE-1 absolute gate, not a relative
     * bar, so it cannot chop a genuine quiet follow-up. */
    vad->speech_idle_ms += vad->frame_dur;
    if(vad->state == ARF_STATE_ACTIVE && above_offset) {
        if(!vad->speech_ref_set || power_db > vad->speech_ref_db) {
            vad->speech_ref_db  = power_db;
            vad->speech_ref_set = 1;
        }
        else {
            vad->speech_ref_db += SPEECH_REF_DECAY * (power_db - vad->speech_ref_db);
        }
        vad->speech_idle_ms = 0;
    }
    else if(vad->speech_ref_set && vad->speech_idle_ms >= SPEECH_LEAVE_MS) {
        vad->speech_ref_set = 0;   /* stale near talker gone -> re-arm PHASE-1 gate */
    }

    /* Adaptive proximity: maintain the background ("far") cluster. Any speech-level
     * frame sitting clearly below the near talker is background -- overlap leaking
     * through during near speech, or a lone far burst -- so track it with a slow
     * EWMA (a single frame cannot swing it). With no such frame for FAR_LEAVE_MS
     * the background is deemed gone and the cluster is released, so it stops gating
     * later/quieter near speech (a talker that left). Because the near reference
     * above always snaps to the LOUDEST talker, a background that was talking from
     * the very start settles into this far cluster the moment the louder near
     * talker speaks -- the onset gate then rejects it with no explicit promotion.
     * Guarded by adaptive_margin_db => default behaviour byte-identical. */
    if(vad->adaptive_margin_db > 0.0) {
        vad->far_idle_ms += vad->frame_dur;
        if(vad->speech_ref_set && above_offset &&
           power_db < vad->speech_ref_db - PROX_ASSIGN_BAND_DB) {
            if(!vad->far_ref_set)
                vad->far_ref_db = power_db;
            else
                vad->far_ref_db += FAR_TRACK_COEF * (power_db - vad->far_ref_db);
            vad->far_ref_set = 1;
            vad->far_idle_ms = 0;
        }
        else if(vad->far_ref_set && vad->far_idle_ms >= FAR_LEAVE_MS) {
            vad->far_ref_set = 0;   /* background gone -> release the gate */
        }
    }

    /* Adapt the noise floor only on clearly non-speech frames. */
    if(power_db < vad->noise_floor_db + vad->offset_snr && vad->state != ARF_STATE_ACTIVE &&  vad->state != ARF_STATE_ONSET) {
        arf_vad_update_noise_floor(vad, power_db);
    }

    switch(vad->state) {
        case ARF_STATE_IDLE:
            if(above_onset) {
                vad->state = ARF_STATE_ONSET;
                vad->duration = vad->frame_dur;
            }
            else {
                vad->idle_duration += vad->frame_dur;
                if(vad->idle_duration >= vad->noinput_timeout) {
                    det_event = ARF_VAD_EVENT_NOINPUT;
                    vad->idle_duration = 0; /* report once per timeout window */
                }
            }
            break;

        case ARF_STATE_ONSET:
            /* Leaky integrator: speech accrues, silence drains (no hard reset),
             * so brief inter-phoneme dips do not abort the onset. */
            if(above_offset) {
                vad->duration += vad->frame_dur;
                if(vad->duration >= vad->speech_timeout) {
                    vad->state = ARF_STATE_ACTIVE;
                    vad->duration = 0;
                    vad->idle_duration = 0;
                    det_event = ARF_VAD_EVENT_ACTIVITY;
                }
            }
            else {
                if(vad->duration <= vad->frame_dur) {
                    vad->state = ARF_STATE_IDLE;
                    vad->duration = 0;
                }
                else {
                    vad->duration -= vad->frame_dur;
                }
            }
            break;

        case ARF_STATE_ACTIVE:
            if(!above_offset) {
                vad->state = ARF_STATE_OFFSET;
                vad->duration = vad->frame_dur;
            }
            break;

        case ARF_STATE_OFFSET:
            /* Symmetric leaky integrator for the trailing-silence decision. */
            if(!above_offset) {
                vad->duration += vad->frame_dur;
                if(vad->duration >= vad->silence_timeout) {
                    vad->state = ARF_STATE_IDLE;
                    vad->duration = 0;
                    vad->idle_duration = 0;
                    det_event = ARF_VAD_EVENT_INACTIVITY;
                }
            }
            else {
                /* speech resumed before the gap qualified as end-of-speech */
                if(vad->duration <= vad->frame_dur) {
                    vad->state = ARF_STATE_ACTIVE;
                    vad->duration = 0;
                }
                else {
                    vad->duration -= vad->frame_dur;
                }
            }
            break;
    }

    /* Throttled debug heartbeat straight from the VAD: shows which stage we are
     * in (and how long we have accrued in it) plus the energy picture. If the
     * detector hangs, the last line printed is the stage it got stuck in. */
    if(++vad->log_frames % ARF_VAD_LOG_EVERY == 0) {
        ARF_VAD_LOG("arf_vad: state=%s(%zums) level=%.1f floor=%.1f SNR=%.1f dB | "
                "phase=%s speech_ref=%.1f far_ref=%.1f%s adaptive_bar=%.1f run=%zu",
                arf_vad_state_str(vad), vad->duration,
                power_db, vad->noise_floor_db, power_db - vad->noise_floor_db,
                vad->speech_ref_set ? "ADAPTIVE" : "FLOOR",
                vad->speech_ref_set ? vad->speech_ref_db : 0.0,
                vad->far_ref_set ? vad->far_ref_db : 0.0,
                vad->far_ref_set ? "" : "(unset)",
                vad->far_ref_set ? vad->far_ref_db + vad->adaptive_margin_db : 0.0,
                vad->strict_run);
    }

    return det_event;
}
