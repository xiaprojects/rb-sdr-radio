# RB-SDR-Radio

Part of RB-Avionics. Turns a spare RTL-SDR dongle into an airband receiver: AM voice on
118 to 137 MHz, played through ALSA and saved as WAV files, controlled from RB-01 over
the bridge.

> **Supplemental, non-required equipment.** A receive-only monitor, not a certified COM
> radio. It does not replace the aircraft's radio.

> **Status: tried on one real R820T, indoors, and not yet on an FC0012.** FM stations and
> an AM carrier were received on the R820T; voice on airband has not been confirmed. The
> rest of the chain is tested on synthetic signals. See
> [What has been verified](#what-has-been-verified).

## Features

- AM demodulation for the 25 kHz and 8.33 kHz channel plans, with the right filter chosen
  from the channel name
- R820T and FC0012 tuners, detected automatically, with settings per tuner
- Squelch in dB above a measured noise floor, unaffected by tuner gain or by continuous
  stations such as ATIS
- ALSA output with software volume; volume 0 releases the sound device
- The SDR is released whenever no frequency is selected, like rb-vor
- One WAV per transmission, with a size limit and recovery after a power cut
- Radio and channel status published on the bridge
- `test-fm` and `test-am` command-line antenna tests

## Install

```bash
git clone https://github.com/xiaprojects/rb-sdr-radio && cd rb-sdr-radio
sudo ./install.sh
```

Installs to `/opt/rb-sdr-radio`, with the config in `/etc/rb-sdr-radio/rb-sdr-radio.json`.
The service is enabled but not started, because the dongle should be pinned first.

## First run

```bash
cd /opt/rb-sdr-radio
python3 -m rbsdrradio self-test        # the software alone, no hardware
python3 -m rbsdrradio devices          # lists index, serial and tuner of each dongle
```

### Choose the dongle

In the config, under `sdr`:

```json
"device_serial": "00000102",
"device_index": 0
```

`device_serial` wins when set. A wrong serial is an error that lists what is present; it
never falls back to an index. Use a serial if there is more than one dongle, because
index order follows USB enumeration and can change between boots. Dongles that share a
serial can be told apart with `rtl_eeprom -d <index> -s <name>`.

### Tuners

There is nothing to select. librtlsdr identifies the chip when the dongle is opened, and
the radio applies the matching entry of `sdr.tuner_profiles`, so one config serves either
dongle. `devices` shows what each dongle has, and the daemon logs it when it opens one.
A dongle that another program holds shows as `in use`.

| | R820T (also R820T2, R860) | FC0012 |
|---|---|---|
| Gain steps | 29, from 0 to 49.6 dB | 5: -9.9, -4.0, 7.1, 17.9, 19.2 dB |
| Shipped gain | 32.8 dB | 19.2 dB, its maximum |
| `gain_mode: agc` | tuner AGC | not available: warns and uses manual gain |
| Receiver type | low IF | zero IF |
| Spectral sense | normal, measured | not measured yet: `test-fm` reports it |

A gain that is not one of the tuner's steps moves to the nearest step, with a warning. A
value in a tuner's profile replaces the same value in the `sdr` block, and the profile is
the place for `gain_db`, `ppm` and `swap_iq`, because the other dongle may want different
ones. A dongle whose chip librtlsdr does not recognise is refused and shows as
`sdrradiofault` 2. Other tuners librtlsdr knows open with the `sdr` block's values and have
not been tested.

The FC0012 has two limits to know about. Its gain stops at 19.2 dB, and its sensitivity on
airband has not been measured. Being zero IF, it can also show a faint image of a very
strong transmitter 508 or 517 kHz below an 8.33 kHz channel, loud enough in simulation to
open the squelch on an empty channel if the tuner's image rejection is poor. That figure
is assumed, not measured. The 25 kHz channels are not affected.

### Test the antenna

```bash
sudo systemctl stop rb-sdr-radio                  # the daemon holds the dongle
python3 -m rbsdrradio test-fm 101.5               # a loud local FM station, heard as music
python3 -m rbsdrradio test-am 118.7               # an airband channel, ideally ATIS
```

Each plays through ALSA, prints a level line every second, and ends with a verdict:

```
    12 s  signal  -23.4 dBFS  noise  -58.0 dBFS  S/N  34.6 dB  [#################   ]
  ...
  signal      -23.1 dBFS (peak -21.8)
  noise       -58.0 dBFS in the channel
  S/N         34.6 dB (median)
  ADC clip    0.000 % of samples
  RESULT: GOOD signal
```

Exit code 0 means a signal was heard, 1 means none, 3 means a hardware problem.

| Option | Use |
|---|---|
| `--seconds 60` | length of the test, default 30 |
| `--volume 30` | 0 to 100, default 50 |
| `--no-audio` | measure only, leave ALSA alone |
| `--wav out.wav` | also save what is heard |
| `--gain 28` / `--gain auto` | try another tuner gain, replacing the profile's |
| `--ppm 12` | frequency correction, replacing the profile's |
| `--device radio` | another dongle, by index or serial |
| `--squelch 10` | `test-am` only, default 0 (open) |
| `--replay x.cu8` | analyse a capture instead of the SDR |

Start with `test-fm`. A station is always on air, so it separates "the antenna works"
from "nobody was transmitting". On airband only an always-on station gives a clear
answer. An FM band-stop filter of the kind rb-vor uses blocks `test-fm` by design.

`test-am` also estimates the dongle's ppm error from the carrier, for `sdr.ppm`. Both
tests warn when the ADC clips.

Both finish by retuning 170 kHz and watching which way the band moves. That shows whether
the dongle is inverted, which otherwise looks just like a dead antenna. The result is
`sense normal`, or an instruction to set `"swap_iq": true` in the tuner profile. It needs
something steady on air, so an empty airband channel gives `not measured`: use `test-fm`.
`--replay` has one tuning only and does not report it.

### Start the service

```bash
sudo systemctl start rb-sdr-radio
journalctl -u rb-sdr-radio -f
```

Test without the RB-01 screen:

```bash
curl http://192.168.10.1/bridge/float -X POST -d '{"sdrradiofreq":118.7,"sdrradiovolume":50}'
curl http://192.168.10.1/bridge/float
curl http://192.168.10.1/bridge/float -X POST -d '{"sdrradiofreq":0}'
```

## Bridge interface

Full specification: [docs/RB-SDR-RADIO-IF-v1.0.md](docs/RB-SDR-RADIO-IF-v1.0.md).

| Command | Meaning |
|---|---|
| `sdrradiofreq` | channel in MHz; 0 is off and releases the SDR |
| `sdrradiovolume` | 0 to 100; 0 releases the ALSA device |
| `sdrradiosquelch` | dB above the noise; 0 holds it open |
| `sdrradiorecord` | 0 or 1 |

Status: `sdrradiostatus`, `sdrradiofault`, `sdrradiofreqactive`, `sdrradiovolumeactive`,
`sdrradiosquelchactive`, `sdrradiorecordactive`, `sdrradiosquelchopen`, `sdrradiosignal`,
`sdrradiosnr`, `sdrradiosdr`, `sdrradioaudio`, `sdrradiorecording`.

## Configuration

`config/rb-sdr-radio.json` holds every default. An unknown key or a bad value stops the
daemon with a message naming it. After changing `record.dir`, also change
`ReadWritePaths` in the service unit.

| Key | Default | Meaning |
|---|---|---|
| `bridge.ws_url`, `bridge.rest_url` | `192.168.10.1` | where RB-01 is |
| `sdr.device_serial`, `sdr.device_index` | unset, 0 | which dongle |
| `sdr.gain_db`, `sdr.gain_mode` | 32.8, manual | tuner gain, or `agc` (not on an FC0012) |
| `sdr.ppm` | 0 | frequency correction |
| `sdr.tuner_profiles` | FC0012, R820T | per-tuner `gain_db`, `ppm`, `swap_iq`, applied automatically |
| `radio.volume`, `radio.squelch_db` | 50, 10 | until RB-01 sets them |
| `audio.device` | `default` | ALSA device |
| `record.enabled`, `record.dir` | true, `/opt/stratux/www/playback` | WAV output |
| `record.max_total_mb`, `record.min_free_mb` | 128, 100 | hard limits on the folder |

## Living with the rest of the aircraft

**Dongle allocation is a matter of configuration.** Stratux gives each dongle tagged
`stratux:1090`, `stratux:978`, `stratux:868` or `stratux:162` to that service, then hands
any other dongle to an enabled service still without one. So give every enabled Stratux
service its own tagged dongle and turn off the services you do not use, and the radio's
dongle stays free. A dongle taken by mistake shows as `sdrradiofault` 2.

**ALSA is shared.** `audio.device` defaults to `default`, the same as rbvoice's output.
If that device is not shared, the radio and voice alerts compete for it, and the loser
shows as `sdrradiofault` 4. Mix them with a `dmix` device such as the `mixout` in
`~/.asoundrc`, using `"device": "plug:mixout"`.

**Recordings live in `/opt/stratux/www/playback`**, the folder RB-01 serves and rbvoice
writes to. On an RB image it is a symlink to dedicated USB storage, made by the builder or
the image build script, so recordings stay off the boot partition. `install.sh` only
checks that the service user can write there. With a larger stick, raise
`record.max_total_mb`. If the stick is missing, recording shows as `sdrradiofault` 8 and
the radio keeps playing.

## When something is wrong

| Symptom | Cause | Fix |
|---|---|---|
| `sdrradiostatus` 1, `already in use` | another program has the dongle | `systemctl stop rb-sdr-radio` for a test; see Stratux above |
| `usb_claim_interface error -6` | kernel DVB driver | reboot, or `sudo rmmod dvb_usb_rtl28xxu` |
| `no supported tuner` | librtlsdr does not recognise the chip | use another dongle |
| `has no AGC` in the log | `gain_mode` is `agc` on an FC0012 | none needed; it uses the profile's manual gain |
| `test-fm` good, `test-am` silent | nothing on air, or wrong band antenna | use ATIS; try the channel with `--seconds 120` |
| Tunes but hears nothing, RTL-SDR Blog V4 | stock librtlsdr | install the vendor's fork |
| Verdict says `swap_iq` | inverted dongle | add `"swap_iq": true` to its tuner profile |
| `sense not measured` | nothing steady on air | repeat with `test-fm` on a strong station |
| Voices clipped on 8.33 kHz channels | dongle far off frequency | set `sdr.ppm` from the `test-am` estimate |
| `ADC is clipping` | gain too high, or a strong nearby transmitter | lower `--gain`, then `sdr.gain_db` |
| Recordings missing in RB-01 | the list does not show `radio_*` yet | known, TD1 in [docs/tech-debt.md](docs/tech-debt.md); the files are in the folder |
| Screen still shows a radio that lost power | no keep-alive yet | known, TD2 in [docs/tech-debt.md](docs/tech-debt.md) |
| `sdrradiofault` 8 | folder missing, read-only or full, or USB storage not mounted | check `record.dir`, the symlink and `ReadWritePaths` |

## Layout

```
rbsdrradio/   controller, dsp, squelch, sdr, audio, recorder, publisher, bridge_client,
              bench (antenna tests), selftest, synth (test signals)
tools/        mock_bridge.py, an RB-01 stand-in for work without one
tests/        run with `pytest`; no dongle, sound card or network needed
docs/         the interface specification and the tech debt register
config/       shipped defaults
```

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/pytest && .venv/bin/ruff check .
```

To run against the stand-in, start `tools/mock_bridge.py --port 8099` and point `ws_url`
and `rest_url` in a copy of the config at `localhost:8099`.

## What has been verified

| | How | Real hardware? |
|---|---|---|
| Demodulation, squelch, filters | synthetic IQ through an 8-bit ADC model | no |
| Bridge | a mock copying RB-01's source, not RB-01 itself | no |
| ALSA | the real `aplay` on the ALSA `null` device | partly, no speakers |
| librtlsdr binding | real librtlsdr 2.0.2 on an R820T; compiled stand-in for the FC0012 | R820T yes, FC0012 no |
| Daemon as real processes, with `curl` and `SIGTERM` | stand-in `librtlsdr.so.0` streaming IQ | no |
| FM on air | R820T, broadcast stations, indoors | yes |
| AM on air | R820T, carrier received on 124.200 MHz, real transmissions, indoors | carrier yes, voice not confirmed |
| Squelch opening on real AM | offline replay of that capture; not yet seen live | partly |
| Spectral sense | R820T: normal on `test-fm` and on recorded airband spectra; a deliberately wrong `swap_iq` was caught. No inverted dongle has been seen | yes |
| Gain and sensitivity in a cockpit | not yet | **no** |
| FC0012 | ladder, AGC and tuning read from the librtlsdr 2.0.2 source, stand-in, simulated zero-IF artefacts | **no** |
| CPU on a Raspberry Pi | not yet; 4.5 % of a core on x86 | **no** |
| systemd unit, `install.sh` on a Pi | syntax-checked only | **no** |
