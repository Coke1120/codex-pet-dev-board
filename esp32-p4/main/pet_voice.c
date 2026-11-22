#include "pet_voice.h"

#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include <time.h>

#include "bootloader_random.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "esp_netif.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "mbedtls/md.h"
#include "mbedtls/platform_util.h"
#include "mbedtls/sha256.h"
#include "mbedtls/x509_crt.h"
#include "nvs_flash.h"
#include "pet_protocol.h"
#include "pet_voice_capture.h"
#include "pet_wireless.h"

/* Factory provisioning in the default NVS partition, namespace `pet_voice`:
 *   bridge_host  string   DNS name matching the HTTPS certificate, up to 253 bytes
 *   bridge_port  u16      optional HTTPS port, defaults to 443
 *   device_id    string   1-32 ASCII alphanumeric, underscore or hyphen
 *   hmac_key     blob     exactly 32 raw bytes, unique per device
 *   ca_pem       string   trusted bridge CA PEM, NVS limit 4000 bytes including NUL
 * The firmware opens this namespace read-only. The clock comes from the
 * existing local USB host sync. The NVS partition's at-rest protection is
 * determined by the device's factory security configuration.
 */
#define VOICE_NVS "pet_voice"
#define VOICE_PATH "/v1/voice-messages"
#define WAV_HEADER_BYTES 44U
#define WAV_CAPACITY (WAV_HEADER_BYTES + PET_VOICE_MAX_PCM_BYTES)
#define VOICE_DEVICE_ID_MAX 32U
#define VOICE_KEY_BYTES 32U
#define VOICE_NONCE_BYTES 16U
#define VOICE_SHA256_BYTES 32U
#define VOICE_CLOCK_MIN_EPOCH INT64_C(1704067200)  /* 2024-01-01 UTC */
#define VOICE_CLOCK_MAX_AGE_US INT64_C(300000000)
#define VOICE_RELEASE_GRACE_MS 5000U
#define VOICE_CONNECT_TIMEOUT_MS 7000
#define VOICE_UPLOAD_TIMEOUT_MS 8000
#define VOICE_RESPONSE_TIMEOUT_MS 45000
#define VOICE_UPLOAD_CHUNK_BYTES 4096U
#define VOICE_NOTIFY_PRESS (1U << 0)
#define VOICE_NOTIFY_END (1U << 1)

typedef struct {
    nvs_handle_t nvs;
    uint16_t port;
    char device[VOICE_DEVICE_ID_MAX + 1];
    char host[254];
    char ca_pem[4000];
    size_t ca_length;
} voice_bridge_t;

typedef struct {
    char timestamp[21];
    char nonce[2 * VOICE_NONCE_BYTES + 1];
    char body_sha256[2 * VOICE_SHA256_BYTES + 1];
    char signature[2 * VOICE_SHA256_BYTES + 1];
} voice_headers_t;

typedef enum {
    HOLD_PRESSED,
    HOLD_RELEASED,
    HOLD_CANCELLED,
} voice_hold_t;

static portMUX_TYPE voice_lock = portMUX_INITIALIZER_UNLOCKED;
static TaskHandle_t voice_task_handle;
static pet_voice_status_t voice_status = {PET_VOICE_STARTING, "Starting voice..."};
static voice_hold_t voice_hold = HOLD_CANCELLED;
static int64_t hold_deadline_us;
static int64_t clock_synced_us;

static void set_status(pet_voice_phase_t phase, const char *message)
{
    taskENTER_CRITICAL(&voice_lock);
    voice_status = (pet_voice_status_t){phase, message};
    taskEXIT_CRITICAL(&voice_lock);
}

pet_voice_status_t pet_voice_get_status(void)
{
    taskENTER_CRITICAL(&voice_lock);
    pet_voice_status_t status = voice_status;
    taskEXIT_CRITICAL(&voice_lock);
    return status;
}

void pet_voice_sync_clock(int64_t unix_epoch)
{
    int64_t synchronized_at = 0;
    if (unix_epoch >= VOICE_CLOCK_MIN_EPOCH && unix_epoch <= PET_MAX_UNIX_EPOCH) {
        const struct timeval now = {.tv_sec = (time_t)unix_epoch};
        if (settimeofday(&now, NULL) == 0) synchronized_at = esp_timer_get_time();
    }
    taskENTER_CRITICAL(&voice_lock);
    clock_synced_us = synchronized_at;
    taskEXIT_CRITICAL(&voice_lock);
}

bool pet_voice_clock_ready(void)
{
    taskENTER_CRITICAL(&voice_lock);
    int64_t last_sync = clock_synced_us;
    taskEXIT_CRITICAL(&voice_lock);
    return last_sync > 0 && esp_timer_get_time() - last_sync <= VOICE_CLOCK_MAX_AGE_US;
}

void pet_voice_press(void)
{
    int64_t deadline = esp_timer_get_time() + PET_VOICE_MAX_SECONDS * INT64_C(1000000);
    TaskHandle_t task = NULL;
    taskENTER_CRITICAL(&voice_lock);
    if (voice_task_handle != NULL && pet_voice_accepts_press(voice_status.phase)) {
        voice_hold = HOLD_PRESSED;
        hold_deadline_us = deadline;
        voice_status = (pet_voice_status_t){PET_VOICE_RECORDING, "Recording... release to send"};
        task = voice_task_handle;
    }
    taskEXIT_CRITICAL(&voice_lock);
    if (task != NULL) xTaskNotify(task, VOICE_NOTIFY_PRESS, eSetBits);
}

void pet_voice_end_press(bool send)
{
    TaskHandle_t task = NULL;
    taskENTER_CRITICAL(&voice_lock);
    if (voice_hold == HOLD_PRESSED && (voice_status.phase == PET_VOICE_RECORDING ||
                                       voice_status.phase == PET_VOICE_RELEASE_TO_SEND)) {
        voice_hold = send ? HOLD_RELEASED : HOLD_CANCELLED;
        task = voice_task_handle;
    }
    taskEXIT_CRITICAL(&voice_lock);
    if (task != NULL) xTaskNotify(task, VOICE_NOTIFY_END, eSetBits);
}

static uint32_t capture_remaining_ms(void)
{
    taskENTER_CRITICAL(&voice_lock);
    int64_t deadline = voice_hold == HOLD_PRESSED ? hold_deadline_us : 0;
    taskEXIT_CRITICAL(&voice_lock);
    int64_t remaining = deadline - esp_timer_get_time();
    return remaining > 0 ? (uint32_t)(remaining / 1000) : 0;
}

static voice_hold_t get_hold(void)
{
    taskENTER_CRITICAL(&voice_lock);
    voice_hold_t hold = voice_hold;
    taskEXIT_CRITICAL(&voice_lock);
    return hold;
}

static bool released_to_send(void)
{
    if (get_hold() == HOLD_PRESSED) {
        set_status(PET_VOICE_RELEASE_TO_SEND, "10 s limit - release to send");
        xTaskNotifyWait(0, UINT32_MAX, NULL, pdMS_TO_TICKS(VOICE_RELEASE_GRACE_MS));
    }
    return get_hold() == HOLD_RELEASED;
}

static bool ascii_alnum(char ch)
{
    return (ch >= 'A' && ch <= 'Z') || (ch >= 'a' && ch <= 'z') || (ch >= '0' && ch <= '9');
}

static bool valid_device_id(const char *device)
{
    if (*device == '\0') return false;
    for (; *device != '\0'; ++device) {
        if (!ascii_alnum(*device) && *device != '_' && *device != '-') return false;
    }
    return true;
}

static bool valid_bridge_host(const char *host)
{
    for (const char *label = host; ; ++host) {
        if (*host == '\0' || *host == '.') {
            if (host == label || host - label > 63 ||
                !ascii_alnum(*label) || !ascii_alnum(host[-1])) return false;
            if (*host == '\0') return true;
            label = host + 1;
        } else if (!ascii_alnum(*host) && *host != '-') {
            return false;
        }
    }
}

static bool load_bridge(voice_bridge_t *bridge)
{
    size_t host_size = sizeof(bridge->host);
    size_t device_size = sizeof(bridge->device);
    size_t key_size = 0;
    bridge->ca_length = sizeof(bridge->ca_pem);
    const esp_err_t port_result = nvs_get_u16(bridge->nvs, "bridge_port", &bridge->port);
    if (port_result == ESP_ERR_NVS_NOT_FOUND) bridge->port = 443;
    if ((port_result != ESP_OK && port_result != ESP_ERR_NVS_NOT_FOUND) || bridge->port == 0 ||
        nvs_get_str(bridge->nvs, "bridge_host", bridge->host, &host_size) != ESP_OK ||
        !valid_bridge_host(bridge->host) ||
        nvs_get_str(bridge->nvs, "device_id", bridge->device, &device_size) != ESP_OK ||
        !valid_device_id(bridge->device) ||
        nvs_get_blob(bridge->nvs, "hmac_key", NULL, &key_size) != ESP_OK ||
        key_size != VOICE_KEY_BYTES ||
        nvs_get_str(bridge->nvs, "ca_pem", bridge->ca_pem, &bridge->ca_length) != ESP_OK ||
        strncmp(bridge->ca_pem, "-----BEGIN CERTIFICATE-----", 27) != 0) {
        return false;
    }

    mbedtls_x509_crt ca;
    mbedtls_x509_crt_init(&ca);
    int result = mbedtls_x509_crt_parse(&ca, (const uint8_t *)bridge->ca_pem, bridge->ca_length);
    mbedtls_x509_crt_free(&ca);
    return result == 0;
}

static const char *preflight_error(void)
{
    if (!pet_voice_clock_ready()) return "Waiting for USB clock sync";

    pet_wireless_snapshot_t wireless;
    if (!pet_wireless_get_snapshot(&wireless) || wireless.backend != PET_WIRELESS_BACKEND_READY ||
        wireless.wifi != PET_WIRELESS_WIFI_CONNECTED) return "Connect Wi-Fi to send voice";

    // The wireless snapshot sees association before the station obtains an address.
    esp_netif_t *station = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
    esp_netif_ip_info_t ip;
    if (station == NULL || !esp_netif_is_netif_up(station) ||
        esp_netif_get_ip_info(station, &ip) != ESP_OK || ip.ip.addr == 0) {
        return "Wi-Fi is waiting for an IP address";
    }
    return NULL;
}

static void write_le32(uint8_t *destination, uint32_t value)
{
    for (size_t i = 0; i < 4; ++i) {
        destination[i] = (uint8_t)value;
        value >>= 8;
    }
}

static void write_wav_header(uint8_t *wav, uint32_t pcm_bytes)
{
    static const uint8_t header[WAV_HEADER_BYTES] = {
        'R', 'I', 'F', 'F', 0, 0, 0, 0,
        'W', 'A', 'V', 'E', 'f', 'm', 't', ' ', 16, 0, 0, 0,
        1, 0, 1, 0, /* PCM, mono */
        0, 0, 0, 0, /* Sample rate */
        0, 0, 0, 0, /* Byte rate */
        PET_VOICE_SAMPLE_BYTES, 0, 8 * PET_VOICE_SAMPLE_BYTES, 0,
        'd', 'a', 't', 'a', 0, 0, 0, 0,
    };
    memcpy(wav, header, sizeof(header));
    write_le32(wav + 4, pcm_bytes + WAV_HEADER_BYTES - 8);
    write_le32(wav + 24, PET_VOICE_SAMPLE_RATE);
    write_le32(wav + 28, PET_VOICE_SAMPLE_RATE * PET_VOICE_SAMPLE_BYTES);
    write_le32(wav + 40, pcm_bytes);
}

static void lower_hex(const uint8_t *bytes, size_t size, char *hex)
{
    static const char digits[] = "0123456789abcdef";
    for (size_t i = 0; i < size; ++i) {
        hex[2 * i] = digits[bytes[i] >> 4];
        hex[2 * i + 1] = digits[bytes[i] & 0x0FU];
    }
    hex[2 * size] = '\0';
}

static bool sign_wav(const voice_bridge_t *bridge, const uint8_t *wav, size_t wav_size,
                     voice_headers_t *headers)
{
    int64_t now = (int64_t)time(NULL);
    if (!pet_voice_clock_ready() || now < VOICE_CLOCK_MIN_EPOCH || now > PET_MAX_UNIX_EPOCH) {
        return false;
    }

    uint8_t hash[VOICE_SHA256_BYTES];
    if (mbedtls_sha256(wav, wav_size, hash, 0) != 0) return false;
    lower_hex(hash, sizeof(hash), headers->body_sha256);

    uint8_t nonce[VOICE_NONCE_BYTES];
    esp_fill_random(nonce, sizeof(nonce));
    lower_hex(nonce, sizeof(nonce), headers->nonce);
    snprintf(headers->timestamp, sizeof(headers->timestamp), "%" PRId64, now);

    char signed_text[sizeof("POST\n" VOICE_PATH "\n\n\n\n") + VOICE_DEVICE_ID_MAX +
                     sizeof(headers->timestamp) + sizeof(headers->nonce) + sizeof(headers->body_sha256)];
    const int length = snprintf(signed_text, sizeof(signed_text), "POST\n" VOICE_PATH "\n%s\n%s\n%s\n%s",
                                bridge->device, headers->timestamp, headers->nonce, headers->body_sha256);
    if (length < 0 || (size_t)length >= sizeof(signed_text)) return false;

    const mbedtls_md_info_t *sha256 = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
    uint8_t key[VOICE_KEY_BYTES];
    size_t key_size = sizeof(key);
    const bool signed_ok = sha256 != NULL &&
        nvs_get_blob(bridge->nvs, "hmac_key", key, &key_size) == ESP_OK && key_size == sizeof(key) &&
        mbedtls_md_hmac(sha256, key, key_size, (const uint8_t *)signed_text, (size_t)length, hash) == 0;
    mbedtls_platform_zeroize(key, sizeof(key));
    if (signed_ok) lower_hex(hash, sizeof(hash), headers->signature);
    mbedtls_platform_zeroize(hash, sizeof(hash));
    return signed_ok;
}

static esp_http_client_handle_t open_upload(const voice_bridge_t *bridge, const uint8_t *wav,
                                            size_t wav_size)
{
    voice_headers_t headers;
    if (!sign_wav(bridge, wav, wav_size, &headers)) return NULL;

    const esp_http_client_config_t config = {
        .host = bridge->host,
        .port = bridge->port,
        .path = VOICE_PATH,
        .transport_type = HTTP_TRANSPORT_OVER_SSL,
        .method = HTTP_METHOD_POST,
        .cert_pem = bridge->ca_pem,
        .cert_len = bridge->ca_length,
        .timeout_ms = VOICE_CONNECT_TIMEOUT_MS,
        .disable_auto_redirect = true,
        .skip_cert_common_name_check = false,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (client == NULL) return NULL;
    const bool headers_ok =
        esp_http_client_set_header(client, "Connection", "close") == ESP_OK &&
        esp_http_client_set_header(client, "Content-Type", "audio/wav") == ESP_OK &&
        esp_http_client_set_header(client, "X-Voice-Device", bridge->device) == ESP_OK &&
        esp_http_client_set_header(client, "X-Voice-Timestamp", headers.timestamp) == ESP_OK &&
        esp_http_client_set_header(client, "X-Voice-Nonce", headers.nonce) == ESP_OK &&
        esp_http_client_set_header(client, "X-Voice-SHA256", headers.body_sha256) == ESP_OK &&
        esp_http_client_set_header(client, "X-Voice-Signature", headers.signature) == ESP_OK;
    mbedtls_platform_zeroize(&headers, sizeof(headers));
    // The streaming API sends one request without automatic redirect or auth handling.
    if (headers_ok && esp_http_client_open(client, (int)wav_size) == ESP_OK) return client;
    esp_http_client_cleanup(client);
    return NULL;
}

static bool write_upload(esp_http_client_handle_t client, const uint8_t *wav, size_t wav_size)
{
    const int64_t deadline = esp_timer_get_time() + VOICE_UPLOAD_TIMEOUT_MS * INT64_C(1000);
    while (wav_size > 0) {
        const int64_t remaining_ms = (deadline - esp_timer_get_time()) / 1000;
        const size_t chunk = wav_size < VOICE_UPLOAD_CHUNK_BYTES ? wav_size : VOICE_UPLOAD_CHUNK_BYTES;
        if (remaining_ms <= 0 ||
            esp_http_client_set_timeout_ms(client, (int)remaining_ms) != ESP_OK ||
            esp_http_client_write(client, (const char *)wav, (int)chunk) != (int)chunk) return false;
        wav += chunk;
        wav_size -= chunk;
    }
    return true;
}

static void discard_wav(uint8_t *wav)
{
    mbedtls_platform_zeroize(wav, WAV_CAPACITY);
    free(wav);
}

static void upload_recording(const voice_bridge_t *bridge, uint8_t *wav, size_t pcm_size)
{
    const char *error = preflight_error();
    if (error != NULL) {
        discard_wav(wav);
        set_status(PET_VOICE_ERROR, error);
        return;
    }

    set_status(PET_VOICE_UPLOADING, "Uploading voice...");
    write_wav_header(wav, (uint32_t)pcm_size);
    /* The C6 has its own RNG. Enable local P4 SAR entropy for the nonce and
     * TLS with the ES8311 capture stopped. This firmware does not use P4 ADC.
     */
    bootloader_random_enable();
    const size_t wav_size = WAV_HEADER_BYTES + pcm_size;
    esp_http_client_handle_t client = open_upload(bridge, wav, wav_size);
    const bool sent = client != NULL && write_upload(client, wav, wav_size);
    discard_wav(wav);

    bool submitted = false;
    if (sent) {
        set_status(PET_VOICE_PROCESSING, "Processing on bridge...");
        const bool received = esp_http_client_set_timeout_ms(client, VOICE_RESPONSE_TIMEOUT_MS) == ESP_OK &&
            esp_http_client_fetch_headers(client) >= 0;
        submitted = received && esp_http_client_get_status_code(client) == 202;
        error = received ? "Bridge rejected voice message" : "No confirmation from bridge";
    } else {
        error = client == NULL ? "Could not open secure voice request" : "Voice upload failed";
    }

    if (client != NULL) esp_http_client_cleanup(client);
    bootloader_random_disable();
    if (submitted) set_status(PET_VOICE_SUBMITTED, "Submitted to Hermes");
    else set_status(PET_VOICE_ERROR, error);
}

static void record_press(const voice_bridge_t *bridge)
{
    const char *error = preflight_error();
    if (error != NULL) {
        set_status(PET_VOICE_ERROR, error);
        return;
    }
    uint8_t *wav = heap_caps_malloc(WAV_CAPACITY, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (wav == NULL) {
        set_status(PET_VOICE_ERROR, "Not enough PSRAM for voice");
        return;
    }

    size_t pcm_size;
    bool stopped;
    esp_err_t result = pet_voice_capture(wav + WAV_HEADER_BYTES, &pcm_size, &stopped,
                                         capture_remaining_ms);
    if (stopped && result == ESP_OK && pcm_size != 0 && released_to_send()) {
        upload_recording(bridge, wav, pcm_size);
        return;
    }
    discard_wav(wav);

    if (!stopped) set_status(PET_VOICE_UNAVAILABLE, "Microphone shutdown failed");
    else if (get_hold() == HOLD_CANCELLED) set_status(PET_VOICE_READY, "Recording cancelled");
    else if (result != ESP_OK) set_status(PET_VOICE_ERROR, "Microphone recording failed");
    else if (pcm_size == 0) set_status(PET_VOICE_ERROR, "Hold longer to record");
    else set_status(PET_VOICE_ERROR, "Recording expired - hold again");
}

static void voice_task(void *argument)
{
    (void)argument;
    voice_bridge_t *bridge = calloc(1, sizeof(*bridge));
    bool provisioned = false;
    if (bridge != NULL && nvs_flash_init() == ESP_OK &&
        nvs_open(VOICE_NVS, NVS_READONLY, &bridge->nvs) == ESP_OK) {
        provisioned = load_bridge(bridge);
        if (!provisioned) nvs_close(bridge->nvs);
    }
    if (!provisioned) {
        free(bridge);
        set_status(PET_VOICE_UNAVAILABLE, "Factory voice setup required");
        vTaskDelete(NULL);
        return;
    }

    set_status(PET_VOICE_READY, "Ready - up to 10 s");
    for (;;) {
        uint32_t notification;
        if (xTaskNotifyWait(0, UINT32_MAX, &notification, portMAX_DELAY) == pdTRUE &&
            (notification & VOICE_NOTIFY_PRESS) != 0) record_press(bridge);
    }
}

void pet_voice_start(void)
{
    TaskHandle_t task = NULL;
    if (xTaskCreate(voice_task, "pet_voice", 8192, NULL, 4, &task) != pdPASS) {
        set_status(PET_VOICE_UNAVAILABLE, "Voice worker unavailable");
        return;
    }
    taskENTER_CRITICAL(&voice_lock);
    voice_task_handle = task;
    taskEXIT_CRITICAL(&voice_lock);
}
