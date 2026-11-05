# RB-SDR-RADIO-IF v1.0: interface specification

**Status:** v1.0, implemented by `rb-sdr-radio` 0.1.0
**For:** whoever builds the radio screen in RB-01. Nothing else is needed to write a
consumer.

RB-SDR-Radio turns a spare RTL-SDR into an airband receiver. It has no screen of its
own: RB-01 sets a few bridge values, and the daemon reports back on the same bridge.

---

## 1. Transport

Both directions go through the existing float bridge.

| Direction | How |
|---|---|
| RB-01 → daemon | `POST /bridge/float` with a JSON object of numbers |
| Daemon → RB-01 | The daemon POSTs its status to the same endpoint; RB-01 reads it from `/bridge/float/ws`, or `GET /bridge/float` |

```bash
curl http://192.168.10.1/bridge/float -X POST -d '{"sdrradiofreq":118.7,"sdrradiovolume":50}'
curl http://192.168.10.1/bridge/float
```

Every value is a number, because RB-01 decodes the body into `map[string]float32`.
**One string, boolean or nested value and RB-01 discards the whole POST**, with only a
line in its log. `null` decodes to 0.

The bridge keeps commands in memory. The daemon reads them with a `GET` whenever it
connects, so it picks up a frequency set before it started. They do not survive an
RB-01 restart, so RB-01 should send them again at start-up if it wants them restored.

---

## 2. Commands

Send only what changes. An absent key means no change, and repeating a value does
nothing.

| Key | Unit | Range | Meaning |
|---|---|---|---|
| `sdrradiofreq` | MHz | 118.000 to 136.990, or 0 | Channel name to receive. **0 is off: the SDR is released.** |
| `sdrradiovolume` | | 0 to 100 | Audio output. **0 releases the ALSA device**; reception and recording carry on. |
| `sdrradiosquelch` | dB | 0 to 100 | Opens when `sdrradiosnr` reaches this value. 0 holds it open, the "squelch test" position. |
| `sdrradiorecord` | | 0 or 1 | Save received audio to the recording folder. |

Defaults until a command arrives come from the daemon's configuration: volume 50,
squelch 10 dB, recording on.

### 2.1 `sdrradiofreq` is a channel name

Send what the pilot dials, not the frequency it tunes. On the 8.33 kHz plan the two
differ, and the name also selects the narrower receive filter.

| Send | Tunes | Filter |
|---|---|---|
| `118.000` | 118.0000 MHz | 25 kHz |
| `118.005` | 118.0000 MHz | 8.33 kHz |
| `118.010` | 118.0083 MHz | 8.33 kHz |
| `118.015` | 118.0167 MHz | 8.33 kHz |
| `118.025` | 118.0250 MHz | 25 kHz |
| `118.030` | 118.0250 MHz | 8.33 kHz |

Valid last two digits: 00 05 10 15 25 30 35 40 50 55 60 65 75 80 85 90. **Names ending
20, 45, 70 and 95 do not exist**, so a 5 kHz spinner must skip them. Anything else
(a true frequency such as 118.0083, or 137.000, the band edge) is rejected: the SDR is
released and `sdrradiofault` bit 1 is set until a valid value arrives.

Changes are debounced by 300 ms, so a tuning knob retunes once when it stops. Expect
`sdrradiofreqactive` to follow about 0.5 s after the last change.

---

## 3. Status

The daemon publishes all twelve keys together. All are integers.

| Key | Unit | Meaning |
|---|---|---|
| `sdrradiostatus` | code | What the radio is doing, see 3.1. The field to drive a status light. |
| `sdrradiofault` | bits | What is wrong, see 3.2. 0 is healthy. |
| `sdrradiofreqactive` | kHz | Channel in use, as a name: 118030 is 118.030. 0 when none. |
| `sdrradiovolumeactive` | | Volume in force, after clamping. |
| `sdrradiosquelchactive` | dB | Squelch in force, after clamping. |
| `sdrradiorecordactive` | 0/1 | Recording enabled. |
| `sdrradiosquelchopen` | 0/1 | Channel activity: the squelch is open. |
| `sdrradiosignal` | dBFS | Channel level, negative. 0 means nothing measured. |
| `sdrradiosnr` | dB | Channel level above the noise floor. 0 on an empty channel. |
| `sdrradiosdr` | 0/1 | The dongle is held. |
| `sdrradioaudio` | 0/1 | The ALSA device is held. |
| `sdrradiorecording` | 0/1 | A file is being written now. |

`sdrradiofreq` is in MHz and `sdrradiofreqactive` in kHz, the same as `vorfreq` and
`vorfreqactive`. Divide by 1000 to display it, and use three decimals.

Before any measurement, and during a retune, `sdrradiosignal` and `sdrradiosnr` read 0,
so a reading always belongs to the channel shown beside it.

### 3.1 `sdrradiostatus`

| Code | Meaning | Dongle held |
|---|---|---|
| 0 | Off: no frequency selected, or the one sent was rejected | no |
| 1 | Fault: a frequency is selected but the SDR is unavailable. Retried every 5 s. | no |
| 2 | Tuning | yes |
| 3 | Listening: tuned, squelch closed | yes |
| 4 | Receiving: tuned, squelch open | yes |

With the squelch held open (`sdrradiosquelch` 0) the status is always 4, even on an
empty channel. Show `sdrradiosnr` if you need to tell those apart.

### 3.2 `sdrradiofault`

A bit mask, because the outputs fail independently: a dead sound card still records,
and a full card still plays.

| Bit | Value | Meaning | What the radio does |
|---|---|---|---|
| 0 | 1 | `sdrradiofreq` is not a channel | Off until a valid value arrives |
| 1 | 2 | SDR unavailable: missing, unplugged or in use by another program | Retries every 5 s |
| 2 | 4 | ALSA device unavailable, for example busy | Keeps receiving and recording, retries every 5 s |
| 3 | 8 | Cannot record: folder missing, read-only or too full | Keeps playing, retries at the next transmission |

Bits 2 and 3 clear themselves when the output is no longer wanted.

### 3.3 Update rate

A change of status, fault, frequency, setting or held resource is sent at once. The
two meters are limited to one update per 0.5 s, and only when they move by 1 dB. The
whole payload is repeated every 5 s, which rebuilds the keys after RB-01 restarts.

**There is no liveness signal yet.** RB-01 keeps the last values for ever and broadcasts
only values that changed, so the repeat reaches no client, and a daemon that lost power
leaves its last `sdrradiostatus` on the bridge. A clean stop publishes status 0, but a
power cut cannot. A keep-alive for all radios is planned: TD2 in [tech-debt.md](tech-debt.md).

---

## 4. Resource rules

| Situation | SDR | ALSA |
|---|---|---|
| Frequency 0 or absent | released | released |
| Invalid frequency | released | released |
| Valid frequency, volume 0 | held | released |
| Valid frequency, volume above 0 | held | held |
| SDR fault | released, retried | released |
| Bridge lost for 5 s | released | released |

After a lost bridge returns, the daemon rereads the commands and resumes by itself.

---

## 5. Recordings

With recording on, each transmission becomes a file in `/opt/stratux/www/playback`, the
folder `/playback` serves. Pauses under 2 s stay in one file, anything under 0.5 s is
dropped, and a long transmission is split every 5 minutes. Nothing is recorded while the
squelch is held open, since it would be hiss.

```
radio_20260607_051225_118030000.wav     YYYYMMDD_HHMMSS in UTC, channel name in Hz
```

WAV, 16 kHz, 16-bit, mono, so a browser `<audio>` plays it directly. The existing
`Size / 30000` estimate in `radio.js` reads about 6 % long. The last field is the
channel name rather than the tuned frequency, so the existing parser in `radio.js`
shows the 118.030 the pilot selected.

The oldest recordings are deleted once they pass 128 MB (about 70 minutes), or sooner if
the partition drops below 100 MB free. Files in the folder from other programs are
never touched.

The folder only stores the recordings. Showing `radio_*` files in the RB-01 list is still
to do: TD1 in [tech-debt.md](tech-debt.md).

---

## 6. Screen checklist

- Frequency selector that skips names ending 20, 45, 70 and 95, and that sends 0 for off.
- Volume and squelch sliders, with a "squelch test" position at 0.
- Show `sdrradiofreqactive`, not the value you sent: it is what the radio is really on.
- Status light from `sdrradiostatus`, with activity from `sdrradiosquelchopen`.
- Signal bar from `sdrradiosnr`, drawn on the same scale as the squelch, so the pilot can
  see where the threshold sits.
- A message for each `sdrradiofault` bit.
- `radio_*` recordings in the playback list (TD1 in [tech-debt.md](tech-debt.md)).

---

## 7. Versioning

Additive keys only within v1.x. Consumers ignore keys they do not know. The bridge
carries every addon's keys, and the daemon does the same.

## 8. Limits

Supplemental, non-required equipment. A receive-only monitor, not a certified COM radio
and no substitute for one.
