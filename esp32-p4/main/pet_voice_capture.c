#include "pet_voice_capture.h"

#include "bsp/esp-bsp.h"
#include "driver/i2c_master.h"
#include "driver/i2s_std.h"

#define ES8311_ADDR 0x18U  /* Seven-bit address, 0x30 in esp_codec_dev. */
#define ES8311_CHIP_ID_REG 0xFDU
#define CODEC_I2C_TIMEOUT_MS 20U
#define I2S_READ_TIMEOUT_MS 40U
#define I2S_FRAMES_PER_BUFFER (PET_VOICE_SAMPLE_RATE / 100U)
#define I2S_BYTES_PER_BUFFER (I2S_FRAMES_PER_BUFFER * PET_VOICE_SAMPLE_BYTES)
#define I2S_STARTUP_BUFFERS 4U

typedef struct {
    uint8_t address;
    uint8_t value;
} codec_register_t;

/* ES8311 datasheet rev 10 and Espressif esp_codec_dev's 4.096 MHz/16 kHz
 * table. The vendor's JC4880P443C-I-W uses the analog Mic1 pair. Keep the
 * DAC in reset, gated, and powered down. GPIO9 (TX) and GPIO11 (PA) are unused.
 */
static const codec_register_t codec_start[] = {
    {0x00, 0x1F}, /* Hold digital blocks in reset. */
    {0x0D, 0xFC}, /* Analog blocks off during configuration. */
    {0x0E, 0x6A}, /* PGA and ADC modulator off. */
    {0x12, 0x02}, /* DAC and its output reference off. */
    {0x00, 0x81}, /* Start codec in slave mode, keep DAC in reset. */
    {0x09, 0x4C}, /* Disabled DAC serial input. */
    {0x0A, 0x0C}, /* ADC serial output, Philips I2S, 16 bits. */
    {0x44, 0x08}, /* ADC in both slots, I2C noise immunity, no routing to DAC. */
    {0x02, 0x00}, /* MCLK predivide and multiply by one. */
    {0x03, 0x10}, /* ADC single speed, 64x oversampling. */
    {0x05, 0x00}, /* ADC clock divide by one. */
    {0x06, 0x03}, /* Clock table BCLK divider. P4 supplies BCLK in slave mode. */
    {0x07, 0x00}, /* LRCK divider high byte. */
    {0x08, 0xFF}, /* LRCK divider low byte (256 MCLK cycles per frame). */
    {0x0B, 0x00}, /* Power-up timing A/B. */
    {0x0C, 0x00}, /* Power-up timing B/C. */
    {0x10, 0x1F}, /* Reference and bias settings from the codec reference. */
    {0x11, 0x7F}, /* Internal reference trim from the codec reference. */
    {0x14, 0x1A}, /* Analog Mic1 with 30 dB PGA gain. */
    {0x15, 0x40}, /* ADC gain ramp. */
    {0x16, 0x24}, /* ADC filter synchronization and gain. */
    {0x17, 0xBF}, /* ADC digital volume. */
    {0x01, 0x3A}, /* Enable MCLK, BCLK and ADC clocks. */
    {0x0E, 0x02}, /* Enable analog PGA and ADC modulator. */
    {0x0D, 0x09}, /* Charge VMID and enable ADC bias, DAC reference off. */
};

static const codec_register_t codec_stop[] = {
    {0x0E, 0x6A}, /* Power down PGA and ADC modulator. */
    {0x0D, 0xFC}, /* Power down analog bias and ADC reference. */
    {0x0A, 0x4C}, /* Mute serial output. */
    {0x00, 0x1F}, /* Stop the state machine and reset digital blocks. */
    {0x01, 0x00}, /* Gate the codec clocks. */
};

static esp_err_t codec_write(i2c_master_dev_handle_t codec, codec_register_t reg)
{
    const uint8_t bytes[] = {reg.address, reg.value};
    return i2c_master_transmit(codec, bytes, sizeof(bytes), CODEC_I2C_TIMEOUT_MS);
}

static esp_err_t read_pcm(i2s_chan_handle_t rx, uint8_t *pcm, size_t *used,
                          uint32_t (*remaining_ms)(void))
{
    unsigned int startup_buffers = I2S_STARTUP_BUFFERS;
    while (*used < PET_VOICE_MAX_PCM_BYTES) {
        uint32_t budget = remaining_ms();
        if (budget == 0U) break;
        uint32_t timeout = budget < I2S_READ_TIMEOUT_MS ? budget : I2S_READ_TIMEOUT_MS;
        size_t received = 0;
        esp_err_t result = i2s_channel_read(rx, pcm + *used, I2S_BYTES_PER_BUFFER,
                                            &received, timeout);
        /* A block that straddles release or the deadline stays out of the WAV. */
        if (remaining_ms() == 0U) break;
        if (result != ESP_OK) return result;
        if (received != I2S_BYTES_PER_BUFFER) return ESP_FAIL;
        if (startup_buffers != 0U) {
            --startup_buffers;
        } else {
            *used += received;
        }
    }
    return ESP_OK;
}

esp_err_t pet_voice_capture(uint8_t *pcm, size_t *used, bool *stopped,
                           uint32_t (*remaining_ms)(void))
{
    *used = 0;
    *stopped = true;
    if (remaining_ms() == 0U) return ESP_OK;

    /* The pinned BSP's display brings up GT911 on its GPIO7/GPIO8 I2C0.
     * Add a device to that bus for this press. The BSP owns the bus lifetime.
     */
    i2c_master_bus_handle_t bus = bsp_i2c_get_handle();
    if (bus == NULL) return ESP_ERR_INVALID_STATE;

    const i2c_device_config_t codec_config = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = ES8311_ADDR,
        .scl_speed_hz = 100000,
    };
    i2c_master_dev_handle_t codec = NULL;
    esp_err_t result = i2c_master_bus_add_device(bus, &codec_config, &codec);
    if (result != ESP_OK) return result;

    i2s_chan_handle_t rx = NULL;
    bool rx_enabled = false;
    bool codec_configured = false;
    const uint8_t chip_id[] = {0x83, 0x11};
    for (size_t i = 0; i < sizeof(chip_id); ++i) {
        uint8_t reg = (uint8_t)(ES8311_CHIP_ID_REG + i);
        uint8_t id;
        result = i2c_master_transmit_receive(codec, &reg, 1, &id, 1, CODEC_I2C_TIMEOUT_MS);
        if (result != ESP_OK) goto cleanup;
        if (id != chip_id[i]) {
            result = ESP_ERR_NOT_FOUND;
            goto cleanup;
        }
    }
    if (remaining_ms() == 0U) goto cleanup;

    /* Vendor board pinout and pinned BSP agree: 13 MCLK, 12 BCLK, 10 WS,
     * 48 ADC data. The P4 is I2S master and the ES8311 is slave.
     */
    i2s_chan_config_t channel_config = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    channel_config.dma_desc_num = 4;
    channel_config.dma_frame_num = I2S_FRAMES_PER_BUFFER;
    i2s_std_config_t stream_config = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(PET_VOICE_SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT,
                                                        I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = BSP_I2S_MCLK,
            .bclk = BSP_I2S_SCLK,
            .ws = BSP_I2S_LCLK,
            .dout = I2S_GPIO_UNUSED,
            .din = BSP_I2S_DSIN,
        },
    };
    stream_config.slot_cfg.slot_mask = I2S_STD_SLOT_LEFT;
    result = i2s_new_channel(&channel_config, NULL, &rx);
    if (result != ESP_OK) goto cleanup;
    result = i2s_channel_init_std_mode(rx, &stream_config);
    if (result != ESP_OK || remaining_ms() == 0U) goto cleanup;
    result = i2s_channel_enable(rx);
    if (result != ESP_OK) goto cleanup;
    rx_enabled = true;

    for (size_t i = 0; i < sizeof(codec_start) / sizeof(codec_start[0]); ++i) {
        if (remaining_ms() == 0U) goto cleanup;
        codec_configured = true;
        result = codec_write(codec, codec_start[i]);
        if (result != ESP_OK) goto cleanup;
    }
    result = read_pcm(rx, pcm, used, remaining_ms);

cleanup:
    /* Cut off I2S sampling immediately. MCLK remains until channel deletion
     * so the ES8311 can still process the power-down writes.
     */
    if (rx_enabled && i2s_channel_disable(rx) != ESP_OK) *stopped = false;
    if (codec_configured) {
        for (size_t i = 0; i < sizeof(codec_stop) / sizeof(codec_stop[0]); ++i) {
            if (codec_write(codec, codec_stop[i]) != ESP_OK) *stopped = false;
        }
    }
    if (rx != NULL && i2s_del_channel(rx) != ESP_OK) *stopped = false;
    if (i2c_master_bus_rm_device(codec) != ESP_OK) *stopped = false;
    return result;
}
