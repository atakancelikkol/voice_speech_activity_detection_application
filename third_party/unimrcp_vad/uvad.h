/*
 * Copyright 2008-2015 Arsen Chaloyan
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/*
 * uvad — standalone extraction of the UniMRCP activity detector
 * (libs/mpf/src/mpf_activity_detector.c). The detection algorithm is
 * unchanged. Mechanical modifications only:
 *  - apr_pool allocation replaced with malloc/free (uvad_create/uvad_destroy)
 *  - apr_size_t / apr_int16_t replaced with size_t / int16_t
 *  - mpf_frame_t replaced with a plain (samples, count) pair; the
 *    MEDIA_FRAME_TYPE_AUDIO check became a count != 0 check
 *  - CODEC_FRAME_TIME_BASE inlined as UVAD_FRAME_TIME_BASE
 *  - apt_log include and the disabled log block removed
 *  - uvad_level() exported so callers can plot the energy curve
 *  - empty-frame guard added to uvad_level()
 */

#ifndef UVAD_H
#define UVAD_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/** Frame duration in ms (from UniMRCP CODEC_FRAME_TIME_BASE) */
#define UVAD_FRAME_TIME_BASE 10

/** Opaque activity detector */
typedef struct uvad_t uvad_t;

/** Events of activity detector (mirrors mpf_detector_event_e) */
typedef enum {
	UVAD_EVENT_NONE,       /**< no event occurred */
	UVAD_EVENT_ACTIVITY,   /**< voice activity (start of speech) */
	UVAD_EVENT_INACTIVITY, /**< voice inactivity (end of speech) */
	UVAD_EVENT_NOINPUT     /**< no input detected */
} uvad_event_e;

/** Create activity detector */
uvad_t* uvad_create(void);

/** Destroy activity detector */
void uvad_destroy(uvad_t *detector);

/** Reset activity detector */
void uvad_reset(uvad_t *detector);

/** Set threshold of voice activity (silence) level */
void uvad_level_threshold_set(uvad_t *detector, size_t level_threshold);

/** Set noinput timeout (ms) */
void uvad_noinput_timeout_set(uvad_t *detector, size_t noinput_timeout);

/** Set timeout required to trigger speech (transition from inactive to active state), ms */
void uvad_speech_timeout_set(uvad_t *detector, size_t speech_timeout);

/** Set timeout required to trigger silence (transition from active to inactive state), ms */
void uvad_silence_timeout_set(uvad_t *detector, size_t silence_timeout);

/** Set frame duration (ms) */
void uvad_frame_duration_set(uvad_t *detector, size_t frame_duration);

/** Process one frame of 16-bit linear PCM audio */
uvad_event_e uvad_process(uvad_t *detector, const int16_t *samples, size_t count);

/** Mean absolute level of a frame (the detector's energy measure) */
size_t uvad_level(const int16_t *samples, size_t count);

#ifdef __cplusplus
}
#endif

#endif /* UVAD_H */
