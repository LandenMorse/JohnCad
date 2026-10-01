// Printer Controller firmware — Arduino UNO R4 WiFi
//
// Polls the JohnCad server's job queue and services THREE printer stations,
// each with its own SPI microSD slot (CS + card-detect pin). Per slot:
//
//   card inserted, slot idle       -> claim next queued job, stream its
//                                      gcode onto the card, POST .../working/
//   card inserted, slot has a job  -> treat as "card came back from the
//                                      printer", POST .../finished/
//
// State (which job is on which slot) is tracked only in RAM. A reset while a
// card is out at a printer loses that slot's assignment — there is no
// on-card marker file by design (see PRINTER_CONTROLLER_API.md discussion).
//
// Libraries needed (Arduino Library Manager):
//   - R4HttpClient   (github.com/piscodev/r4httpclient) — used for the small
//                      JSON queue endpoint only.
//   - ArduinoJson    (arduinojson.org)
//   - WiFiS3 and SD  ship with the UNO R4 board package / Arduino core.
//
// Wiring: all three microSD modules share one SPI bus (MOSI/MISO/SCK), each
// with its own CS pin and its own card-detect (CD) pin. CD is assumed
// active-LOW (reads LOW when a card is present) wired to INPUT_PULLUP —
// check your module and flip CARD_PRESENT_LEVEL below if it's the other way.

#include "secrets.h"
#include <WiFiS3.h>
#include <R4HttpClient.h>
#include <ArduinoJson.h>
#include <SPI.h>
#include <SD.h>

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

const char* WIFI_SSID  = SECRET_SSID;
const char* WIFI_PASS  = SECRET_PASS;
const char* API_TOKEN  = SECRET_API_TOKEN;

// Split out for the raw-socket streaming download (see httpGetToFile below).
// Local dev over plain HTTP: set API_USE_TLS to false and API_PORT to 80.
const char* API_HOST    = "rhsprint.crabdance.com";
const uint16_t API_PORT = 443;
const bool API_USE_TLS  = true;
const char* API_BASE_PATH = "/api"; // no trailing slash

const unsigned long POLL_INTERVAL_MS = 30000;

const int CARD_PRESENT_LEVEL = LOW; // level read on CD pin when a card IS present

struct Slot {
  const char* name;
  uint8_t csPin;
  uint8_t cdPin;
  bool cardPresentLast; // debounced state from previous loop
  bool hasJob;
  int jobId;
};

// TODO: set these to your actual wiring. Every unused CS pin must be pulled
// HIGH (deselected) whenever a *different* slot's card is being accessed,
// since the stock SD library only manages one mounted volume at a time even
// though all three share the same SPI bus — see selectSlot() below.
Slot slots[3] = {
  { "printer1", 4,  5, false, false, -1 },
  { "printer2", 6,  7, false, false, -1 },
  { "printer3", 8,  9, false, false, -1 },
};

WiFiSSLClient sslClient;
R4HttpClient http;

// ---------------------------------------------------------------------------
// WiFi
// ---------------------------------------------------------------------------

void connectWiFi() {
  Serial.print(F("Connecting to WiFi"));
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) {
    delay(1000);
    Serial.print(F("."));
  }
  Serial.println();
  Serial.println(F("Connected."));
}

// ---------------------------------------------------------------------------
// SD slot selection
// ---------------------------------------------------------------------------
// Deselect every other slot's CS line, then mount this slot's card. Call
// this immediately before any SD.* call, since only one card can be mounted
// on the shared SPI bus at a time.
bool selectSlot(int index) {
  for (int i = 0; i < 3; i++) {
    if (i != index) {
      pinMode(slots[i].csPin, OUTPUT);
      digitalWrite(slots[i].csPin, HIGH);
    }
  }
  return SD.begin(slots[index].csPin);
}

bool isCardPresent(int index) {
  return digitalRead(slots[index].cdPin) == CARD_PRESENT_LEVEL;
}

// ---------------------------------------------------------------------------
// Job status transitions (small POSTs, fine to buffer via R4HttpClient)
// ---------------------------------------------------------------------------

bool postJobTransition(int jobId, const char* action) {
  String url = String("https://") + API_HOST + API_BASE_PATH + "/jobs/" + String(jobId) + "/" + action + "/";
  http.begin(sslClient, url);
  http.addHeader("Authorization: Token " + String(API_TOKEN));
  int status = http.POST("");
  http.close();

  if (status == 200) {
    Serial.println(String("Job ") + jobId + " -> " + action + " OK");
    return true;
  }
  Serial.println(String("Job ") + jobId + " -> " + action + " FAILED, status " + status);
  return false;
}

// ---------------------------------------------------------------------------
// Streaming gcode download straight to SD, bypassing R4HttpClient::getBody()
// (which buffers the whole response in a String — fine for the small queue
// JSON, but a sliced gcode file can be hundreds of KB to multiple MB, and the
// R4 only has 32KB of SRAM). This talks to WiFiSSLClient/WiFiClient directly
// and writes each chunk to the SD file as it arrives.
// ---------------------------------------------------------------------------

bool httpGetToFile(const char* path, File& outFile) {
  WiFiClient* rawClient;
  WiFiSSLClient tlsClient;
  WiFiClient plainClient;

  bool connected;
  if (API_USE_TLS) {
    connected = tlsClient.connect(API_HOST, API_PORT);
    rawClient = &tlsClient;
  } else {
    connected = plainClient.connect(API_HOST, API_PORT);
    rawClient = &plainClient;
  }

  if (!connected) {
    Serial.println(F("gcode download: connect failed"));
    return false;
  }

  rawClient->print(String("GET ") + path + " HTTP/1.1\r\n");
  rawClient->print(String("Host: ") + API_HOST + "\r\n");
  rawClient->print(String("Authorization: Token ") + API_TOKEN + "\r\n");
  rawClient->print("Connection: close\r\n");
  rawClient->print("\r\n");

  // --- status line ---
  unsigned long start = millis();
  while (!rawClient->available()) {
    if (millis() - start > 10000) {
      Serial.println(F("gcode download: timed out waiting for response"));
      rawClient->stop();
      return false;
    }
  }
  String statusLine = rawClient->readStringUntil('\n');
  int firstSpace = statusLine.indexOf(' ');
  int statusCode = statusLine.substring(firstSpace + 1, firstSpace + 4).toInt();
  if (statusCode != 200) {
    Serial.println(String("gcode download: HTTP ") + statusCode);
    rawClient->stop();
    return false;
  }

  // --- headers (discarded, just read to the blank line) ---
  while (rawClient->connected() || rawClient->available()) {
    String line = rawClient->readStringUntil('\n');
    if (line == "\r" || line.length() == 0) break;
  }

  // --- body, streamed straight to the SD file ---
  uint8_t buf[512];
  size_t totalBytes = 0;
  while (rawClient->connected() || rawClient->available()) {
    int n = rawClient->available();
    if (n > 0) {
      int toRead = n < (int)sizeof(buf) ? n : (int)sizeof(buf);
      int r = rawClient->read(buf, toRead);
      if (r > 0) {
        outFile.write(buf, r);
        totalBytes += r;
      }
    } else {
      delay(1);
    }
  }
  rawClient->stop();
  Serial.println(String("gcode download: wrote ") + totalBytes + " bytes");
  return totalBytes > 0;
}

// ---------------------------------------------------------------------------
// Queue polling — small JSON, fine to use R4HttpClient's buffered getBody().
// Returns the number of jobs parsed into `ids`/`urls` (capped by maxJobs).
// ---------------------------------------------------------------------------

int fetchQueue(int* ids, String* urls, int maxJobs) {
  String url = String("https://") + API_HOST + API_BASE_PATH + "/jobs/queue/";
  http.begin(sslClient, url);
  http.addHeader("Authorization: Token " + String(API_TOKEN));
  int status = http.GET();

  if (status != 200) {
    Serial.println(String("Queue poll failed: ") + status);
    http.close();
    return 0;
  }

  String body = http.getBody();
  http.close();

  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, body);
  if (err) {
    Serial.println(String("Queue JSON parse failed: ") + err.c_str());
    return 0;
  }

  int count = 0;
  for (JsonObject job : doc.as<JsonArray>()) {
    if (count >= maxJobs) break;
    ids[count] = job["id"];
    urls[count] = String((const char*)job["gcode_download_url"]);
    count++;
  }
  return count;
}

// ---------------------------------------------------------------------------
// Assign the next unclaimed queued job (by array position) to a slot: mount
// its card, stream the gcode down, mark it working.
// ---------------------------------------------------------------------------

bool assignJobToSlot(int slotIndex, int jobId, const String& gcodeUrl) {
  Slot& slot = slots[slotIndex];

  if (!selectSlot(slotIndex)) {
    Serial.println(String(slot.name) + ": SD.begin failed, can't write job " + jobId);
    return false;
  }

  // 8.3-safe filename: J<jobid>.GCO
  String filename = String("J") + jobId + ".GCO";
  File f = SD.open(filename, FILE_WRITE);
  if (!f) {
    Serial.println(String(slot.name) + ": failed to open " + filename + " for write");
    return false;
  }

  // gcode_download_url is absolute; extract just the path portion since
  // httpGetToFile talks to API_HOST directly.
  int pathStart = gcodeUrl.indexOf('/', gcodeUrl.indexOf("://") + 3);
  String path = pathStart >= 0 ? gcodeUrl.substring(pathStart) : gcodeUrl;

  bool ok = httpGetToFile(path.c_str(), f);
  f.close();

  if (!ok) {
    Serial.println(String(slot.name) + ": gcode download failed for job " + jobId);
    SD.remove(filename);
    return false;
  }

  if (!postJobTransition(jobId, "working")) {
    // Card already has the file; the job just didn't flip server-side.
    // Leave slot unassigned so the next insert/poll cycle can retry against
    // whatever state the server is actually in.
    return false;
  }

  slot.hasJob = true;
  slot.jobId = jobId;
  Serial.println(String(slot.name) + ": job " + jobId + " written, marked working");
  return true;
}

// ---------------------------------------------------------------------------
// setup / loop
// ---------------------------------------------------------------------------

void setup() {
  Serial.begin(115200);
  while (!Serial);

  if (WiFi.status() == WL_NO_MODULE) {
    Serial.println(F("WiFi module not found!"));
    while (true);
  }

  for (int i = 0; i < 3; i++) {
    pinMode(slots[i].cdPin, INPUT_PULLUP);
    pinMode(slots[i].csPin, OUTPUT);
    digitalWrite(slots[i].csPin, HIGH);
    slots[i].cardPresentLast = isCardPresent(i);
  }

  connectWiFi();
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    connectWiFi();
  }

  // Figure out which slots need a new job this cycle before hitting the
  // network, so we only poll the queue once per loop.
  bool slotNeedsJob[3];
  int slotsNeedingJobs = 0;
  bool cardPresentNow[3];

  for (int i = 0; i < 3; i++) {
    cardPresentNow[i] = isCardPresent(i);
    bool insertedEdge = cardPresentNow[i] && !slots[i].cardPresentLast;

    if (insertedEdge && slots[i].hasJob) {
      // Card came back from the printer.
      if (postJobTransition(slots[i].jobId, "finished")) {
        slots[i].hasJob = false;
        slots[i].jobId = -1;
      }
      slotNeedsJob[i] = false;
    } else if (insertedEdge && !slots[i].hasJob) {
      slotNeedsJob[i] = true;
      slotsNeedingJobs++;
    } else {
      slotNeedsJob[i] = false;
    }
  }

  if (slotsNeedingJobs > 0) {
    const int MAX_JOBS = 10;
    int ids[MAX_JOBS];
    String urls[MAX_JOBS];
    int count = fetchQueue(ids, urls, MAX_JOBS);

    int nextJob = 0;
    for (int i = 0; i < 3 && nextJob < count; i++) {
      if (slotNeedsJob[i]) {
        assignJobToSlot(i, ids[nextJob], urls[nextJob]);
        nextJob++;
      }
    }
    if (nextJob == 0 && slotsNeedingJobs > 0) {
      Serial.println(F("Card(s) inserted but nothing queued right now."));
    }
  }

  for (int i = 0; i < 3; i++) {
    slots[i].cardPresentLast = cardPresentNow[i];
  }

  delay(POLL_INTERVAL_MS);
}
