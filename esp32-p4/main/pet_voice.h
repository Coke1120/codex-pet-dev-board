#pragma once

#include <stdbool.h>
#include <stdint.h>

typedef enum {
    PET_VOICE_STARTING,
    PET_VOICE_UNAVAILABLE,
    PET_VOICE_READY,
    PET_VOICE_RECORDING,
    PET_VOICE_RELEASE_TO_SEND,
    PET_VOICE_UPLOADING,
    PET_VOICE_PROCESSING,
    PET_VOICE_SUBMITTED,
    PET_VOICE_ERROR,
} pet_voice_phase_t;

typedef struct {
    pet_voice_phase_t phase;
    const char *message;
} pet_voice_status_t;

static inline bool pet_voice_accepts_press(pet_voice_phase_t phase)
{
    return phase == PET_VOICE_READY || phase == PET_VOICE_SUBMITTED || phase == PET_VOICE_ERROR;
}

void pet_voice_start(void);
void pet_voice_sync_clock(int64_t unix_epoch);
bool pet_voice_clock_ready(void);
pet_voice_status_t pet_voice_get_status(void);
void pet_voice_press(void);
void pet_voice_end_press(bool send);
