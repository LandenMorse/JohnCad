# Printer Controller API Guide (Arduino UNO R4 WiFi)

This document is for whoever (or whatever) is building the firmware that runs
on the **printer controller** — an Arduino UNO R4 WiFi sitting at the 3D
printers that pulls approved print `Job`s from the JohnCad server and feeds
their G-code to the printers via microSD card.

## Architecture, in one paragraph

The JohnCad server (a separate machine, on a different network) hosts a
public website where Students upload `.stl` files and, at their discretion,
request a print — that creates a `Job`. An admin reviews pending jobs;
approving one triggers slicing (`.stl` → G-code) and, once slicing succeeds,
the job auto-advances to `queued`. The controller's job is to **poll** the
queue on a timer over WiFi, download each queued job's G-code, write it to a
microSD card when one is detected, mark the job `working`, physically get
that card to a printer, and once the card comes back, mark the job
`finished`. This is deliberately **pull-only** — the controller calls out to
the server, never the reverse — because it's behind a different network's
NAT/router with no public address.

## Hardware / library assumptions

- **Board**: Arduino UNO R4 WiFi (on-board ESP32-S3 as the WiFi radio,
  driven via the `WiFiS3` library that ships with the board package).
- **HTTP client**: [`R4HttpClient`](https://github.com/piscodev/r4httpclient)
  — a small GET/POST wrapper around `WiFiS3`, installable from the Arduino
  Library Manager (search "R4HttpClient").
- **JSON parsing**: [`ArduinoJson`](https://arduinojson.org/) (also in the
  Library Manager) — needed to parse the queue endpoint's JSON array.
- **TLS**: use `WiFiSSLClient` (not plain `WiFiClient`) so `https://` works.

**Real memory constraint, not a hypothetical**: the R4's MCU has only 32KB of
SRAM, and `R4HttpClient::getBody()` buffers the *entire* HTTP response body
into a single `String` in RAM before you can use it. That's fine for the
queue listing (small JSON), but a sliced G-code file can easily be hundreds
of KB to multiple MB — **that will not fit**. Don't build the naive "GET the
gcode endpoint, hold the whole body in a `String`" version for real use;
you'll need either a streaming read off the underlying `WiFiSSLClient`
(bypassing `getBody()`) or a library/approach that writes to the SD card as
bytes arrive rather than buffering first. The example below still shows the
simple (RAM-buffered) version for clarity of the request/response shape —
treat the actual G-code download as the one piece you'll need to redo for
anything beyond a toy file.

## Base URL

All endpoints below are relative to the JohnCad server's public domain, e.g.:

```
https://rhsprint.crabdance.com/api/
```

(Replace with the real domain once it's set up. Local dev uses plain HTTP —
swap `WiFiSSLClient` for plain `WiFiClient` and drop the port/TLS if you're
pointing at a `http://` dev server instead.)

## Authentication

Every endpoint requires a **DRF auth token** sent as an `Authorization` header:

```
Authorization: Token <the-token>
```

The token belongs to a dedicated Django user (e.g. `printer-service`) created
specifically for the controller — it is **not** a real person's login. To get
a token:

1. In the JohnCad admin panel (`/admin/`), create a user for the controller
   and tick **Staff status** (required — the API rejects non-staff users;
   no superuser or other permissions needed). Browser sessions don't work on
   the API, only the token.
2. Go to the **Tokens** section of the admin and create a token for that user.
3. Copy the key and store it in the sketch (or a `secrets.h` you keep out of
   version control, same idea as Arduino's own `R4WiFi_secrets.h` pattern for
   WiFi credentials).

A missing/bad token gets `401 Unauthorized` on every endpoint below.

## Job status lifecycle

`pending → approved → queued → working → finished`, plus `rejected` (an
admin can decline a pending job instead of approving it). The controller
only ever touches the last two transitions — everything up through `queued`
happens on the server (Student requests, admin approves, server slices).

| Status     | Set by            |
|------------|-------------------|
| `pending`  | Student           |
| `approved` | Admin              |
| `queued`   | Server (auto, once slicing succeeds) |
| `working`  | **Controller** (via API) |
| `finished` | **Controller** (via API) |
| `rejected` | Admin              |

## Endpoints

### `GET /api/jobs/queue/`

Lists jobs that are sliced and waiting — "what's ready to print right now,"
in FIFO order (oldest-approved first). Call this on your polling interval.

```
curl -H "Authorization: Token <key>" https://rhsprint.crabdance.com/api/jobs/queue/
```

Response — a JSON array, empty if there's nothing queued:

```json
[
  {
    "id": 7,
    "attachment_name": "Bracket v2",
    "username": "alice",
    "gcode_download_url": "https://rhsprint.crabdance.com/api/jobs/7/gcode/"
  }
]
```

Use `gcode_download_url` directly rather than constructing URLs yourself.

### `GET /api/jobs/<id>/gcode/`

Streams the job's sliced G-code file as an attachment download.

```
curl -H "Authorization: Token <key>" -o job7.gcode \
     https://rhsprint.crabdance.com/api/jobs/7/gcode/
```

- `200` — response body is the G-code file.
- `403` — the job isn't `queued`/`working` (shouldn't happen if you only
  download IDs you got from `/api/jobs/queue/`).
- `404` — no job with that ID.

### `POST /api/jobs/<id>/working/`

Call this once you've started writing the job's G-code to a detected
microSD card. Only valid when the job is currently `queued`.

```
curl -X POST -H "Authorization: Token <key>" \
     https://rhsprint.crabdance.com/api/jobs/7/working/
```

- `200` — `{"status": "ok"}`, job is now `working`.
- `409` — the job wasn't `queued` (e.g. already `working`/`finished`) —
  response body explains the mismatch.

### `POST /api/jobs/<id>/finished/`

Call this once the microSD card has been physically returned to the
controller after being used at the printer. Only valid when the job is
currently `working`.

```
curl -X POST -H "Authorization: Token <key>" \
     https://rhsprint.crabdance.com/api/jobs/7/finished/
```

- `200` — `{"status": "ok"}`, job is now `finished`.
- `409` — the job wasn't `working` — response body explains the mismatch.

Both status-transition endpoints are **not** idempotent the way the old API
was — calling `working` twice, or `finished` before `working`, returns a
409 rather than silently succeeding. Treat a 409 as "something's out of
sync" and don't just retry blindly.

## Recommended polling loop (Arduino C)

```cpp
#include "R4WiFi_secrets.h"   // SECRET_SSID / SECRET_PASS, same as Arduino's own examples
#include <WiFiS3.h>
#include <R4HttpClient.h>
#include <ArduinoJson.h>

WiFiSSLClient client;
R4HttpClient http;

const char* WIFI_SSID = SECRET_SSID;
const char* WIFI_PASS = SECRET_PASS;

const char* BASE_URL = "https://rhsprint.crabdance.com/api";
const char* API_TOKEN = "your-token-here";
const unsigned long POLL_INTERVAL_MS = 30000;

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

void setup() {
  Serial.begin(115200);
  while (!Serial);

  if (WiFi.status() == WL_NO_MODULE) {
    Serial.println(F("WiFi module not found!"));
    while (true);
  }
  connectWiFi();
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    connectWiFi();
  }

  // --- GET /api/jobs/queue/ ---
  http.begin(client, String(BASE_URL) + "/jobs/queue/");
  http.addHeader("Authorization: Token " + String(API_TOKEN));
  int status = http.GET();

  if (status == 200) {
    String body = http.getBody();
    http.close();

    JsonDocument doc;   // ArduinoJson v7: grows on the heap as needed
    DeserializationError err = deserializeJson(doc, body);
    if (!err) {
      for (JsonObject job : doc.as<JsonArray>()) {
        int jobId = job["id"];
        const char* gcodeUrl = job["gcode_download_url"];

        // --- GET the gcode (see the memory-constraint note above --
        //     this buffered version is only safe for small files) ---
        http.begin(client, gcodeUrl);
        http.addHeader("Authorization: Token " + String(API_TOKEN));
        int gcodeStatus = http.GET();
        if (gcodeStatus == 200) {
          String gcode = http.getBody();

          // TODO: write `gcode` to the detected microSD card here

          http.close();

          // --- POST /api/jobs/<id>/working/ ---
          http.begin(client, String(BASE_URL) + "/jobs/" + String(jobId) + "/working/");
          http.addHeader("Authorization: Token " + String(API_TOKEN));
          http.POST("");
          http.close();

          // TODO: once the card is physically returned, call:
          // http.begin(client, String(BASE_URL) + "/jobs/" + String(jobId) + "/finished/");
          // http.addHeader("Authorization: Token " + String(API_TOKEN));
          // http.POST("");
          // http.close();
        } else {
          Serial.println("Gcode download failed: " + String(gcodeStatus));
          http.close();
        }
      }
    } else {
      Serial.println("JSON parse failed: " + String(err.c_str()));
    }
  } else {
    Serial.println("Queue poll failed: " + String(status));
    http.close();
  }

  delay(POLL_INTERVAL_MS);
}
```

Note the real flow is card-driven, not purely time-driven: `working` fires
when you detect a card and start the export; `finished` fires later, on a
separate detection event (the same or a different poll cycle) when that card
comes back. The sketch above is a simplified illustration — your actual
firmware needs to track "which job is on which card" between those two
calls, and should use a non-blocking timing pattern (`millis()`-based)
rather than a blocking `delay()` in `loop()` once you're also polling an SD
card-detect pin or handling other real-time work.

## Things to know

- The queue is **shared across all Students** — there's no per-user or
  per-printer filtering.
- Nothing reaches `queued` automatically — a human admin has to approve each
  job, and slicing has to succeed, so don't build any "auto-print on upload"
  or "auto-print on export" assumption into the controller firmware.
- There's no rate limiting built in; a 30–60 second poll interval is
  reasonable and won't stress the server.
- If a job needs to be pulled back out of the queue for any reason (bad
  slice, Student cancels, etc.), that's currently only possible via the
  Django admin, not this API.
- Keep WiFi credentials and the API token out of version control the same
  way Arduino's own examples do (a `secrets.h`/`R4WiFi_secrets.h` that's
  `.gitignore`d), not hardcoded in a sketch you might publish or share.
