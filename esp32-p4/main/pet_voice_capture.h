#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#define PET_VOICE_SAMPLE_RATE 16000U
#define PET_VOICE_SAMPLE_BYTES 2U
#define PET_VOICE_MAX_SECONDS 10U
#define PET_VOICE_MAX_PCM_BYTES (PET_VOICE_SAMPLE_RATE * PET_VOICE_SAMPLE_BYTES * PET_VOICE_MAX_SECONDS)

/* remaining_ms returns zero once the hold ends or its recording limit is reached. */
esp_err_t pet_voice_capture(uint8_t *pcm, size_t *used, bool *stopped,
                           uint32_t (*remaining_ms)(void));
