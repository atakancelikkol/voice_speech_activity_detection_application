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

/* Derived from UniMRCP libs/mpf/src/mpf_activity_detector.c — see uvad.h
 * for the list of modifications. */

#include <stdlib.h>

#include "uvad.h"

/** Detector states */
typedef enum {
	DETECTOR_STATE_INACTIVITY,           /**< inactivity detected */
	DETECTOR_STATE_ACTIVITY_TRANSITION,  /**< activity detection is in-progress */
	DETECTOR_STATE_ACTIVITY,             /**< activity detected */
	DETECTOR_STATE_INACTIVITY_TRANSITION /**< inactivity detection is in-progress */
} uvad_state_e;

/** Activity detector */
struct uvad_t {
	/* voice activity (silence) level threshold */
	size_t       level_threshold;

	/* period of activity required to complete transition to active state */
	size_t       speech_timeout;
	/* period of inactivity required to complete transition to inactive state */
	size_t       silence_timeout;
	/* noinput timeout */
	size_t       noinput_timeout;

	/* current state */
	uvad_state_e state;
	/* duration spent in current state  */
	size_t       duration;
	/* frame duration  */
	size_t       frame_duration;
};

/** Create activity detector */
uvad_t* uvad_create(void)
{
	uvad_t *detector = malloc(sizeof(uvad_t));
	if(!detector) {
		return NULL;
	}
	detector->level_threshold = 2; /* 0 .. 255 */
	detector->speech_timeout = 300; /* 0.3 s */
	detector->silence_timeout = 300; /* 0.3 s */
	detector->noinput_timeout = 5000; /* 5 s */
	detector->duration = 0;
	detector->frame_duration = UVAD_FRAME_TIME_BASE;
	detector->state = DETECTOR_STATE_INACTIVITY;
	return detector;
}

/** Destroy activity detector */
void uvad_destroy(uvad_t *detector)
{
	free(detector);
}

/** Reset activity detector */
void uvad_reset(uvad_t *detector)
{
	detector->duration = 0;
	detector->state = DETECTOR_STATE_INACTIVITY;
}

/** Set threshold of voice activity (silence) level */
void uvad_level_threshold_set(uvad_t *detector, size_t level_threshold)
{
	detector->level_threshold = level_threshold;
}

/** Set noinput timeout */
void uvad_noinput_timeout_set(uvad_t *detector, size_t noinput_timeout)
{
	detector->noinput_timeout = noinput_timeout;
}

/** Set timeout required to trigger speech (transition from inactive to active state) */
void uvad_speech_timeout_set(uvad_t *detector, size_t speech_timeout)
{
	detector->speech_timeout = speech_timeout;
}

/** Set timeout required to trigger silence (transition from active to inactive state) */
void uvad_silence_timeout_set(uvad_t *detector, size_t silence_timeout)
{
	detector->silence_timeout = silence_timeout;
}

/** Set frame duration */
void uvad_frame_duration_set(uvad_t *detector, size_t frame_duration)
{
	detector->frame_duration = frame_duration;
}

static inline void uvad_state_change(uvad_t *detector, uvad_state_e state)
{
	detector->duration = 0;
	detector->state = state;
}

/** Mean absolute level of a frame */
size_t uvad_level(const int16_t *samples, size_t count)
{
	size_t sum = 0;
	const int16_t *cur = samples;
	const int16_t *end;

	if(!count) {
		return 0;
	}

	end = cur + count;
	for(; cur < end; cur++) {
		if(*cur < 0) {
			sum -= *cur;
		}
		else {
			sum += *cur;
		}
	}

	return sum / count;
}

/** Process current frame */
uvad_event_e uvad_process(uvad_t *detector, const int16_t *samples, size_t count)
{
	uvad_event_e det_event = UVAD_EVENT_NONE;
	size_t level = 0;
	if(count) {
		/* first, calculate current activity level of processed frame */
		level = uvad_level(samples, count);
	}

	if(detector->state == DETECTOR_STATE_INACTIVITY) {
		if(level >= detector->level_threshold) {
			/* start to detect activity */
			uvad_state_change(detector,DETECTOR_STATE_ACTIVITY_TRANSITION);
		}
		else {
			detector->duration += detector->frame_duration;
			if(detector->duration >= detector->noinput_timeout) {
				/* detected noinput */
				det_event = UVAD_EVENT_NOINPUT;
			}
		}
	}
	else if(detector->state == DETECTOR_STATE_ACTIVITY_TRANSITION) {
		if(level >= detector->level_threshold) {
			detector->duration += detector->frame_duration;
			if(detector->duration >= detector->speech_timeout) {
				/* finally detected activity */
				det_event = UVAD_EVENT_ACTIVITY;
				uvad_state_change(detector,DETECTOR_STATE_ACTIVITY);
			}
		}
		else {
			/* fallback to inactivity */
			uvad_state_change(detector,DETECTOR_STATE_INACTIVITY);
		}
	}
	else if(detector->state == DETECTOR_STATE_ACTIVITY) {
		if(level >= detector->level_threshold) {
			detector->duration += detector->frame_duration;
		}
		else {
			/* start to detect inactivity */
			uvad_state_change(detector,DETECTOR_STATE_INACTIVITY_TRANSITION);
		}
	}
	else if(detector->state == DETECTOR_STATE_INACTIVITY_TRANSITION) {
		if(level >= detector->level_threshold) {
			/* fallback to activity */
			uvad_state_change(detector,DETECTOR_STATE_ACTIVITY);
		}
		else {
			detector->duration += detector->frame_duration;
			if(detector->duration >= detector->silence_timeout) {
				/* detected inactivity */
				det_event = UVAD_EVENT_INACTIVITY;
				uvad_state_change(detector,DETECTOR_STATE_INACTIVITY);
			}
		}
	}

	return det_event;
}
