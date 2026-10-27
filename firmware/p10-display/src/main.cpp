// P10 display node for shuttle-speed-cam.
//
// An ESP32 DevKit drives one or two HUB12 P10 panels (32x16, single colour) with
// the DMD32 library and shows whatever the Raspberry Pi sends over UART2.
// HUB12 panels have no memory of their own: rows must be scanned continuously from
// a hardware timer, which is why this lives on a microcontroller and not on the Pi.
//
// Protocol, one ASCII line per command (115200 baud, '\n' terminated):
//   SPEED <kmh> <best>   big number, "KM/H", and the session best
//   TEXT <message>       scrolling message
//   CLEAR                blank the panels
//   TEST                 all LEDs on for a second, then back to what was shown
//   # anything           comment, ignored
//
// Wiring (DMD32 defaults, VSPI):
//   P10 nOE -> GPIO22   P10 A -> GPIO19   P10 B -> GPIO21
//   P10 CLK -> GPIO18   P10 SCLK (latch) -> GPIO2   P10 R (data) -> GPIO23
//   Pi GPIO14 (TXD) -> GPIO16 (RX2), and GND to GND.

#include <Arduino.h>
#include <DMD32.h>

#include "fonts/Arial_black_16.h"
#include "fonts/SystemFont5x7.h"

#ifndef PANELS_ACROSS
#define PANELS_ACROSS 2
#endif
#define PANELS_DOWN 1
#define WIDTH (32 * PANELS_ACROSS)

#define PI_RX_PIN 16
#define PI_TX_PIN 17
#define PI_BAUD 115200

DMD dmd(PANELS_ACROSS, PANELS_DOWN);
hw_timer_t *scanTimer = nullptr;

void IRAM_ATTR onScanTimer() { dmd.scanDisplayBySPI(); }

// ---------------------------------------------------------------------------------
// What is on screen

enum class Mode { Idle, Speed, Text };

Mode mode = Mode::Idle;
int speedKmh = 0;
int bestKmh = 0;
char message[64] = "READY";
uint32_t shownAt = 0;           // when the current speed arrived, for the blink
uint32_t marqueeStepAt = 0;
const uint32_t BLINK_MS = 1200;  // a new speed flashes for this long

// A 5x7 crown, drawn next to the session best.
const uint8_t CROWN[7] = {0b10101, 0b10101, 0b11111, 0b11111, 0b11111, 0b00000, 0b11111};

int textWidth(const char *s) {
  int w = 0;
  for (; *s; s++) w += dmd.charWidth(*s) + 1;
  return w > 0 ? w - 1 : 0;
}

void drawCrown(int x, int y) {
  for (int row = 0; row < 7; row++)
    for (int col = 0; col < 5; col++)
      if (CROWN[row] & (1 << (4 - col))) dmd.writePixel(x + col, y + row, GRAPHICS_NORMAL, 1);
}

void drawSpeed(bool visible) {
  dmd.clearScreen(true);
  if (!visible) return;

  char big[8];
  snprintf(big, sizeof big, "%d", speedKmh);
  dmd.selectFont(Arial_Black_16);

  if (PANELS_ACROSS == 1) {
    // One panel: just the number, centred.
    dmd.drawString((WIDTH - textWidth(big)) / 2, 0, big, strlen(big), GRAPHICS_NORMAL);
    return;
  }

  // Two panels: number on the left, "KM/H" and the best speed on the right.
  const int split = WIDTH - 24;
  dmd.drawString(split - 2 - textWidth(big), 0, big, strlen(big), GRAPHICS_NORMAL);

  dmd.selectFont(System5x7);
  dmd.drawString(WIDTH - 23, 0, "KM/H", 4, GRAPHICS_NORMAL);
  char best[8];
  snprintf(best, sizeof best, "%d", bestKmh);
  drawCrown(WIDTH - 24, 9);
  dmd.drawString(WIDTH - textWidth(best), 9, best, strlen(best), GRAPHICS_NORMAL);
}

void startMarquee() {
  dmd.clearScreen(true);
  dmd.selectFont(Arial_Black_16);
  dmd.drawMarquee(message, strlen(message), WIDTH - 1, 0);
  marqueeStepAt = millis();
}

void testPattern() {
  dmd.drawTestPattern(PATTERN_STRIPE_0);
  delay(500);
  dmd.drawTestPattern(PATTERN_STRIPE_1);
  delay(500);
}

void redraw() {
  if (mode == Mode::Speed) drawSpeed(true);
  else startMarquee();
}

// ---------------------------------------------------------------------------------
// Commands from the Pi

void handleLine(char *line) {
  if (line[0] == '\0' || line[0] == '#') return;

  if (strncmp(line, "SPEED ", 6) == 0) {
    int kmh = 0, best = 0;
    int n = sscanf(line + 6, "%d %d", &kmh, &best);
    if (n < 1 || kmh < 0 || kmh > 999) return;
    speedKmh = kmh;
    bestKmh = (n == 2) ? best : max(bestKmh, kmh);
    mode = Mode::Speed;
    shownAt = millis();
  } else if (strncmp(line, "TEXT ", 5) == 0) {
    strncpy(message, line + 5, sizeof message - 1);
    message[sizeof message - 1] = '\0';
    mode = Mode::Text;
    startMarquee();
  } else if (strcmp(line, "CLEAR") == 0) {
    mode = Mode::Idle;
    message[0] = '\0';
    dmd.clearScreen(true);
  } else if (strcmp(line, "TEST") == 0) {
    testPattern();
    redraw();
  }
}

void readSerial(Stream &port) {
  static char buf[80];
  static size_t len = 0;
  while (port.available()) {
    char c = port.read();
    if (c == '\r') continue;
    if (c == '\n') {
      buf[len] = '\0';
      handleLine(buf);
      len = 0;
    } else if (len < sizeof buf - 1) {
      buf[len++] = c;
    } else {
      len = 0;  // line too long: drop it
    }
  }
}

// ---------------------------------------------------------------------------------

void setup() {
  Serial.begin(115200);  // USB, handy for testing commands by hand
  Serial2.begin(PI_BAUD, SERIAL_8N1, PI_RX_PIN, PI_TX_PIN);

  // Refresh one quarter of the rows every 300 us (Arduino-ESP32 2.x timer API).
  scanTimer = timerBegin(0, ESP.getCpuFreqMHz(), true);  // 1 tick = 1 us
  timerAttachInterrupt(scanTimer, &onScanTimer, true);
  timerAlarmWrite(scanTimer, 300, true);
  timerAlarmEnable(scanTimer);

  dmd.clearScreen(true);
  startMarquee();
  Serial.println("# p10-display ready");
}

void loop() {
  readSerial(Serial2);
  readSerial(Serial);

  uint32_t now = millis();
  if (mode == Mode::Speed) {
    // Flash a fresh speed a few times, then hold it.
    uint32_t age = now - shownAt;
    static bool lastVisible = false;
    static uint32_t lastShownAt = 0;
    bool visible = age >= BLINK_MS || (age / 150) % 2 == 0;
    if (visible != lastVisible || shownAt != lastShownAt) {
      drawSpeed(visible);
      lastVisible = visible;
      lastShownAt = shownAt;
    }
  } else if (mode == Mode::Text && message[0] && now - marqueeStepAt >= 30) {
    marqueeStepAt = now;
    dmd.stepMarquee(-1, 0);  // wraps around by itself
  }
}
