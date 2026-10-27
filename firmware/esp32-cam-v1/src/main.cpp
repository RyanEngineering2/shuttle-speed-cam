// v1 (retired): the whole speed camera on an AI-Thinker ESP32-CAM.
//
// This was the first version of the project: OV2640 camera, motion detection and
// a Kalman filter all on the ESP32, sending the speed to the P10 display node.
// It works, but the ESP32 + OV2640 top out around 30 fps at QVGA, and at 30 fps a
// smash loses about a third of its speed before the camera's second look. That is
// why the project moved to a Raspberry Pi Zero 2 W (see ../../shuttlecam).
//
// Per frame (QVGA, 8-bit grayscale):
//   1. Motion mask: a pixel counts if it differs from the previous frame AND from
//      a slow running background (the same idea as the Pi version).
//   2. Blobs: count motion pixels in 8x8 cells, then flood-fill neighbouring cells.
//   3. Tracking: a constant-velocity Kalman filter per axis, in metres.
//   4. When the track ends, send "SPEED <kmh> <best>" out of the serial port, to
//      the P10 display node (firmware/p10-display). Debug lines start with '#'.

#include <Arduino.h>
#include <math.h>

#include "esp_camera.h"

// ---------------------------------------------------------------------------------
// Settings

// Metres per pixel on the plane of flight, at QVGA. Measure it: put two marks a
// known distance apart along the court centre line and count the pixels.
#define METRES_PER_PIXEL 0.024f

#define DIFF_THRESHOLD 20   // grey levels
#define BG_SHIFT 5          // background follows the scene at 1/32 per frame
#define MIN_BLOB_PIXELS 3
#define MAX_BLOB_PIXELS 250
#define MIN_SPEED_KMH 50.0f
#define MAX_SPEED_KMH 450.0f
#define MIN_HITS 4
#define MAX_MISSES 2
#define PIXEL_NOISE 1.5f    // centroid noise, pixels
#define ACCEL_NOISE 400.0f  // m/s^2; must cover drag, which a CV model ignores

// AI-Thinker ESP32-CAM pin map
#define PWDN_GPIO_NUM 32
#define RESET_GPIO_NUM -1
#define XCLK_GPIO_NUM 0
#define SIOD_GPIO_NUM 26
#define SIOC_GPIO_NUM 27
#define Y9_GPIO_NUM 35
#define Y8_GPIO_NUM 34
#define Y7_GPIO_NUM 39
#define Y6_GPIO_NUM 36
#define Y5_GPIO_NUM 21
#define Y4_GPIO_NUM 19
#define Y3_GPIO_NUM 18
#define Y2_GPIO_NUM 5
#define VSYNC_GPIO_NUM 25
#define HREF_GPIO_NUM 23
#define PCLK_GPIO_NUM 22

static const int W = 320, H = 240;
static const int CELL = 8, CW = W / CELL, CH = H / CELL;

// ---------------------------------------------------------------------------------
// Motion detection

static uint8_t *previous;     // last frame
static uint16_t *background;  // running average, 8.8 fixed point
static uint16_t cellCount[CW * CH];
static uint32_t cellSumX[CW * CH], cellSumY[CW * CH];

struct Blob {
  float x, y;  // centroid, pixels
  int pixels;
};

static int findBlobs(const uint8_t *frame, Blob *out, int maxBlobs) {
  memset(cellCount, 0, sizeof cellCount);
  memset(cellSumX, 0, sizeof cellSumX);
  memset(cellSumY, 0, sizeof cellSumY);

  for (int y = 0; y < H; y++) {
    for (int x = 0; x < W; x++) {
      int i = y * W + x;
      int cur = frame[i];
      int bg = background[i] >> 8;
      bool moving = abs(cur - previous[i]) > DIFF_THRESHOLD && abs(cur - bg) > DIFF_THRESHOLD;
      background[i] += ((cur << 8) - (int)background[i]) >> BG_SHIFT;
      previous[i] = cur;
      if (moving) {
        int c = (y / CELL) * CW + (x / CELL);
        cellCount[c]++;
        cellSumX[c] += x;
        cellSumY[c] += y;
      }
    }
  }

  // Flood-fill touching active cells into blobs.
  static int16_t stack[CW * CH];
  static uint8_t seen[CW * CH];
  memset(seen, 0, sizeof seen);
  int n = 0;
  for (int c0 = 0; c0 < CW * CH && n < maxBlobs; c0++) {
    if (!cellCount[c0] || seen[c0]) continue;
    uint32_t count = 0, sx = 0, sy = 0;
    int top = 0;
    stack[top++] = c0;
    seen[c0] = 1;
    while (top) {
      int c = stack[--top];
      count += cellCount[c];
      sx += cellSumX[c];
      sy += cellSumY[c];
      int cx = c % CW, cy = c / CW;
      for (int dy = -1; dy <= 1; dy++) {
        for (int dx = -1; dx <= 1; dx++) {
          int nx = cx + dx, ny = cy + dy;
          if (nx < 0 || ny < 0 || nx >= CW || ny >= CH) continue;
          int nc = ny * CW + nx;
          if (cellCount[nc] && !seen[nc]) {
            seen[nc] = 1;
            stack[top++] = nc;
          }
        }
      }
    }
    if (count >= MIN_BLOB_PIXELS && count <= MAX_BLOB_PIXELS) {
      out[n++] = {(float)sx / count, (float)sy / count, (int)count};
    }
  }
  return n;
}

// ---------------------------------------------------------------------------------
// Constant-velocity Kalman filter, one per axis: state (position, velocity).

struct Axis {
  float p, v;            // metres, m/s
  float P00, P01, P11;   // covariance

  void init(float pos, float vel, float posVar, float velVar) {
    p = pos;
    v = vel;
    P00 = posVar;
    P01 = 0;
    P11 = velVar;
  }

  void predict(float dt) {
    p += v * dt;
    float q = ACCEL_NOISE * ACCEL_NOISE;
    float dt2 = dt * dt, dt3 = dt2 * dt, dt4 = dt3 * dt;
    P00 += dt * (2 * P01 + dt * P11) + q * dt4 / 4;
    P01 += dt * P11 + q * dt3 / 2;
    P11 += q * dt2;
  }

  void update(float z, float r) {
    float s = P00 + r;
    float k0 = P00 / s, k1 = P01 / s;
    float y = z - p;
    p += k0 * y;
    v += k1 * y;
    float p00 = P00, p01 = P01;
    P00 -= k0 * p00;
    P01 -= k0 * p01;
    P11 -= k1 * p01;
  }
};

struct Track {
  bool active = false;
  Axis ax, ay;
  int hits = 0, misses = 0;
  float peakKmh = 0, path = 0;
  float lastX = 0, lastY = 0;
};

static Track track;
static Blob lastBlobs[8];
static int lastBlobCount = 0;
static float lastTime = 0;
static float bestKmh = 0;

static void toMetres(const Blob &b, float &x, float &y) {
  x = b.x * METRES_PER_PIXEL;
  y = (H - b.y) * METRES_PER_PIXEL;  // y up
}

static void endTrack() {
  if (track.hits >= MIN_HITS && track.path > 0.5f && track.peakKmh >= MIN_SPEED_KMH &&
      track.peakKmh <= MAX_SPEED_KMH) {
    bestKmh = max(bestKmh, track.peakKmh);
    Serial.printf("SPEED %d %d\n", (int)lroundf(track.peakKmh), (int)lroundf(bestKmh));
  }
  track.active = false;
}

static void step(float t, Blob *blobs, int n) {
  const float r = (PIXEL_NOISE * METRES_PER_PIXEL) * (PIXEL_NOISE * METRES_PER_PIXEL);
  float dt = t - lastTime;

  if (track.active && dt > 0) {
    track.ax.predict(dt);
    track.ay.predict(dt);
    // Nearest blob inside a 4-sigma gate.
    float gate = 4 * sqrtf(max(track.ax.P00, track.ay.P00) + r);
    int best = -1;
    float bestDist = gate;
    for (int i = 0; i < n; i++) {
      float x, y;
      toMetres(blobs[i], x, y);
      float d = hypotf(x - track.ax.p, y - track.ay.p);
      if (d < bestDist) {
        bestDist = d;
        best = i;
      }
    }
    if (best >= 0) {
      float x, y;
      toMetres(blobs[best], x, y);
      track.ax.update(x, r);
      track.ay.update(y, r);
      track.path += hypotf(x - track.lastX, y - track.lastY);
      track.lastX = x;
      track.lastY = y;
      track.hits++;
      track.misses = 0;
      if (track.hits >= 3) {
        track.peakKmh = max(track.peakKmh, hypotf(track.ax.v, track.ay.v) * 3.6f);
      }
    } else if (++track.misses > MAX_MISSES) {
      endTrack();
    }
  }

  if (!track.active && dt > 0) {
    // Start a track from a blob pair (last frame -> this frame) at a plausible speed.
    for (int i = 0; i < n && !track.active; i++) {
      for (int j = 0; j < lastBlobCount; j++) {
        float x1, y1, x0, y0;
        toMetres(blobs[i], x1, y1);
        toMetres(lastBlobs[j], x0, y0);
        float vx = (x1 - x0) / dt, vy = (y1 - y0) / dt;
        float kmh = hypotf(vx, vy) * 3.6f;
        if (kmh < MIN_SPEED_KMH || kmh > MAX_SPEED_KMH * 1.2f) continue;
        float velVar = powf(0.3f * hypotf(vx, vy) + 5, 2);
        track = Track();
        track.active = true;
        track.ax.init(x1, vx, r, velVar);
        track.ay.init(y1, vy, r, velVar);
        track.hits = 2;
        track.path = hypotf(x1 - x0, y1 - y0);
        track.lastX = x1;
        track.lastY = y1;
        break;
      }
    }
  }

  memcpy(lastBlobs, blobs, n * sizeof(Blob));
  lastBlobCount = n;
  lastTime = t;
}

// ---------------------------------------------------------------------------------

static bool startCamera() {
  camera_config_t config = {};
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM;
  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;
  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;
  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;
  config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk = XCLK_GPIO_NUM;
  config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM;
  config.pin_href = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn = PWDN_GPIO_NUM;
  config.pin_reset = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_GRAYSCALE;
  config.frame_size = FRAMESIZE_QVGA;
  config.jpeg_quality = 12;
  config.fb_count = 2;
  config.fb_location = CAMERA_FB_IN_PSRAM;
  config.grab_mode = CAMERA_GRAB_LATEST;
  if (esp_camera_init(&config) != ESP_OK) return false;

  // Short manual exposure: less motion blur, at the cost of a darker image.
  sensor_t *s = esp_camera_sensor_get();
  s->set_exposure_ctrl(s, 0);
  s->set_aec_value(s, 80);
  s->set_gain_ctrl(s, 0);
  s->set_agc_gain(s, 20);
  return true;
}

void setup() {
  Serial.begin(115200);  // TX (GPIO1) goes to the display node's RX
  previous = (uint8_t *)ps_malloc(W * H);
  background = (uint16_t *)ps_malloc(W * H * sizeof(uint16_t));
  if (!previous || !background || !startCamera()) {
    Serial.println("# camera or PSRAM init failed");
    while (true) delay(1000);
  }

  // Seed the background with the first frame.
  camera_fb_t *fb = esp_camera_fb_get();
  for (int i = 0; i < W * H; i++) {
    previous[i] = fb->buf[i];
    background[i] = fb->buf[i] << 8;
  }
  esp_camera_fb_return(fb);
  Serial.println("TEXT READY");
}

void loop() {
  static uint32_t frames = 0, since = millis();

  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) return;
  float t = fb->timestamp.tv_sec + fb->timestamp.tv_usec * 1e-6f;
  Blob blobs[8];
  int n = findBlobs(fb->buf, blobs, 8);
  esp_camera_fb_return(fb);
  step(t, blobs, n);

  frames++;
  if (millis() - since >= 5000) {
    Serial.printf("# %.1f fps\n", frames * 1000.0f / (millis() - since));
    frames = 0;
    since = millis();
  }
}
