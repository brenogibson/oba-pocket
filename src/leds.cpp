#include "leds.h"
#include <Adafruit_NeoPixel.h>

static constexpr uint8_t LED_PIN        = 25;
static constexpr uint8_t LED_COUNT      = 10;   // 0-4 lado direito, 5-9 esquerdo (placa na posição padrão)
static constexpr uint8_t LED_BRIGHTNESS = 70;   // 0-255

static Adafruit_NeoPixel leds(LED_COUNT, LED_PIN, NEO_GRB + NEO_KHZ800);

void ledsBegin() {
  leds.begin();
  leds.setBrightness(LED_BRIGHTNESS);
  leds.show();
}

static uint32_t scaled(uint32_t rgb, float b) {
  return leds.Color((rgb >> 16 & 0xFF) * b, (rgb >> 8 & 0xFF) * b, (rgb & 0xFF) * b);
}

void ledsShow(const LedFx& fx, uint32_t now, float t) {
  for (int i = 0; i < LED_COUNT; ++i) {
    uint32_t c = 0;
    switch (fx.kind) {
      case LedFx::Off: break;
      case LedFx::Solid: c = fx.color; break;
      case LedFx::Breathe:
        c = scaled(fx.color, fx.min + (fx.max - fx.min) * (0.5f + 0.5f * sinf(t * fx.speed)));
        break;
      case LedFx::Rainbow:  // speed: quanto o matiz anda por ms
        c = leds.gamma32(leds.ColorHSV((uint16_t)(i * 6553 + now * (uint32_t)fx.speed), 255, 255));
        break;
      case LedFx::Strobe: c = (now / fx.ms) % 2 ? fx.color : 0; break;
      case LedFx::Chase: {
        int pos = (now / fx.ms) % LED_COUNT;
        int d = abs(i - pos);
        d = min(d, LED_COUNT - d);
        c = d == 0 ? fx.color : (d == 1 ? fx.trail : 0);
        break;
      }
    }
    leds.setPixelColor(i, c);
  }
  leds.show();
}
