# RB-SDR-Radio

Airband AM receiver for RB-Avionics: RTL-SDR in, ALSA and WAV out, driven by RB-01's
float bridge. `README.md` is for builders, `docs/RB-SDR-RADIO-IF-v1.0.md` is the contract
with RB-01, whose radio screen is developed in a separate session, and `docs/tech-debt.md`
holds what is known and deferred. Sibling projects are in `../`: `rb-vor` (the pattern this
follows), `roastbeef` (RB-01), `rbvoice`.

## Commands

```bash
.venv/bin/pytest                         # no dongle, sound card or network needed
.venv/bin/ruff check .
python3 -m rbsdrradio self-test
```

## Rules

- `controller.py` stays synchronous and clock-injected. I/O belongs in `daemon.py`.
- Everything on the bridge is an integer (see below). Changing `publisher.FIELDS` means
  changing the interface document too; `tests/test_packaging.py` fails if they differ.
- Nothing may block the sample path: `audio.write` is non-blocking and the recorder works
  behind a queue. Neither output's failure may stop the other or the daemon.
- `recorder.py` deletes only names matching its own pattern. rbvoice shares the folder.
- Frequencies are integers internally. Bridge values are float32.
- Protocol concerns common to all radios, keep-alive first, are designed for all of them
  together (TD2). No fix for this daemon's websocket alone.

## Facts about RB-01 that shaped this (from `roastbeef/main`)

- `handleBridgeFloatSetRequest` decodes `map[string]float32`, all or nothing: one
  non-number silently loses the whole POST. There is no `null`, so off is 0.
- `/bridge/float/ws` sends nothing on connect, only the whole map after a change. Hence
  the `GET` after connecting. rb-vor lacks this and misses commands set before it started.
- No ping, pong or close frame (rb-vor A29), so keepalive is at TCP level.
- Each change is logged and rebroadcast to every client, hence the meter rate limit.
- Only changes are rebroadcast, so a repeated payload reaches no client and nothing shows
  that a device has died. Open for all radios: TD2.
- `/playback` serves `/opt/stratux/www/playback`, which is `record.dir`. `radio.js` lists
  only `vrin*` for now: TD1.
- `sdr.go` `configDevices` hands any untagged dongle to an enabled service without one.
  That is a configuration matter, documented in the README, with nothing to change in code.

## Decisions

- **Tuner detected, never selected.** librtlsdr identifies the chip in `rtlsdr_open` and has
  no call to force one, so a setting could only disagree with the hardware. Per-tuner values
  live in `sdr.tuner_profiles`, keyed by what the dongle reports.
- **An FC0012 gets manual gain even when `agc` is configured.** `fc0012_set_gain_mode` in
  librtlsdr is an empty stub and init leaves register 0x13 on the -4 dB step, so "AGC" would
  be a fixed low gain reported as AGC. From the 2.0.2 source, not measured.
- **A dongle with no recognised tuner is refused.** librtlsdr falls back to direct sampling,
  so blocks arrive and nothing is heard, which looks like a quiet channel.
- **Spectral sense is measured by retuning, never by comparing the two sides.** A retune moves
  a real signal down in a normal device's baseband and up in an inverted one's, while DC and
  spurs stay put. The energy comparison this replaced told a healthy R820T to set `swap_iq`:
  106.7 MHz had faded and 106.2 MHz, a different station, is exactly on its mirror. `swap_iq`
  then appeared to work because it demodulated that station.
- **Python**, like rb-vor. AM costs 1.4 ms per 32 ms block at full clock on x86 (4.5 %).
  `top` shows 20 to 35 % there, but that is the same work at the 500 to 800 MHz `schedutil`
  picks for a light load, not a problem. Not yet measured on a Pi.
- **ctypes, not pyrtlsdr or SoapySDR.** pyrtlsdr fails at import on a stock librtlsdr
  (`rtlsdr_set_dithering`, rb-vor A27) and would need pip on a managed Python.
- **`aplay` child process, not an ALSA binding.** Process exit is an absolute release, and
  a stuck sound device cannot stall the sample path.
- **Noise floor from the median of a periodogram around the channel**, not remembered from
  quiet moments. A remembered floor learns ATIS as noise and mutes it.
- **Software volume.** The sound device is shared; the radio must not move its mixer.
- **WAV at 16 kHz.** `radio.js` estimates length as `Size / 30000`.
- **Recordings in `/opt/stratux/www/playback`.** Builders and the image build script
  symlink it to dedicated USB storage, never the boot partition. Nothing here creates that
  link. `pi` owns the folder, so the service needs no capabilities.
- **Interpretation:** "a command like test" was read as command-line tests, so `test-fm`
  and `test-am` are CLI, not bridge commands.

## Not yet verified on hardware

One real R820T has been run, a Nooelec NESDR Nano 3, indoors on a PC: FM received, sense
normal, an AM carrier received on 124.200 MHz, voice not confirmed. No FC0012 has been
connected. See the table at the end of `README.md`. Still open: whether the FC0012 needs
`swap_iq` (rb-vor O21, `test-fm` reports it), real sensitivity, and whether the default
squelch of 10 dB suits a real cockpit. Indoors it stayed shut on real AM, which is one site
and no reason to change it yet. Sessions may share the dongle: `usb_claim_interface error
-6` with no `Detached kernel driver` line means another program holds it.
