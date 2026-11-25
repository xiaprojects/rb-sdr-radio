# RB-SDR-Radio

Part of RB-Avionics. Turns a spare RTL-SDR dongle into an airband receiver: AM voice on
118 to 137 MHz, and a VOR's ident and voice from 108 MHz, played through ALSA and saved as
WAV files, controlled from RB-01 over the bridge.

> **Supplemental, non-required equipment.** A receive-only monitor, not a certified COM
> radio. It does not replace the aircraft's radio.

> **Status: tried on one real R820T, indoors, and not yet on an FC0012.** FM stations and
> an AM carrier were received on the R820T; voice on airband has not been confirmed. The
> rest of the chain is tested on synthetic signals. See
> [What has been verified](#what-has-been-verified).

## Features

- AM demodulation for the 25 kHz and 8.33 kHz channel plans, with the right filter chosen
  from the channel name, and the 50 kHz grid of the VOR band
- Dual watch: takes turns on two channels with one tuner, the primary always first. See
  [Dual watch](#dual-watch)
- R820T and FC0012 tuners, detected automatically, with settings per tuner
- Squelch on the carrier, in dB above a measured noise floor, unaffected by tuner gain,
  by continuous stations such as ATIS, or by noise that is stronger inside the channel
  than beside it. See [Squelch](#squelch)
- ALSA output with software volume; volume 0 releases the sound device. No sound card,
  as on a Raspberry Pi 5 out of the box: leave `audio.device` empty
- The SDR is released whenever no frequency is selected, like rb-vor
- One WAV per transmission, with a size limit and recovery after a power cut. Nothing from
  the VOR band: a carrier that never stops would push real traffic out of the quota
- Radio and channel status published on the bridge
- `test-fm` and `test-am` command-line antenna tests, and `test-dual` for the tuner

## Install

```bash
git clone https://github.com/xiaprojects/rb-sdr-radio && cd rb-sdr-radio
sudo ./install.sh
```

Installs to `/opt/addons/rb-sdr-radio`, with the config in `/boot/firmware/rb/rb-sdr-radio.json`.
The service is enabled but not started, because the dongle should be pinned first.

## First run

```bash
cd /opt/addons/rb-sdr-radio
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
| `--detector synchronous` | `test-am` only, try the other AM detector |
| `--noise-reduction 12` | `test-am` only, 0 to 20 dB, 0 is off |
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

## Dual watch

`sdrradiofreqsecondary` makes the radio take turns on two channels, silent until one carries
a signal, then locked on it. RB-01 sends it with DUAL on, from the standby channel.

It is watching, not receiving both: one tuner hears one place at a time. The start of a
transmission is clipped by 30 to 125 ms. The primary always wins, and the secondary is a
courtesy: with a busy primary it is heard only part of the time. Where nothing may be lost,
fit a second dongle. [docs/dual-watch-feasibility.md](docs/dual-watch-feasibility.md) has
the reasoning and the measurements.

**On an FC0012 it runs unmeasured**, with a warning in the log. The hop time there is an
estimate from the librtlsdr source, and rb-vor rates that tuner a poor VHF receiver, so
expect more clipped starts and missed calls. To measure one, or any dongle:

```bash
sudo systemctl stop rb-sdr-radio                  # the daemon holds the dongle
python3 -m rbsdrradio test-dual 120.000 124.425 --seconds 600 --squelch 60
```

A squelch above any signal keeps it scanning, so it measures nothing but the hopping. With
a real squelch it locks, and the primary then stops the hops by design. Keep stderr too:
`[R82XX] PLL not locked!` is printed there and reported as success. One at start-up means
nothing, and any others are failed hops. The test exits 0 with no failed hops, overruns or
read errors. On the R820T a full cycle takes about 120 ms.

## The VOR band

From 108.000 to 117.950 on the 50 kHz grid the radio plays a station's ident and voice. It
gives no bearing, that is rb-vor, and nothing is recorded. Two things to know, measured on a
Nooelec Nano 3 with an indoor antenna and no FM filter:

- **FM broadcast can deafen the bottom of the band.** The tuner sits 256 kHz below the channel
  and the ADC sees about 1 MHz, so a strong station near 108 MHz reaches it. At the default
  32.8 dB, tuning 108.000 to 108.500 clipped 69 to 96 % of the samples and read nothing at
  all, 108.750 was 20 dB above the rest, and from 109.000 up it was clean. At 20.7 dB nothing
  clipped. An FM band-stop filter, of the kind rb-vor uses, is the real answer, and
  `sdr.tuner_profiles` holds the gain.
- **115.200 is the dongle.** It is 4 x 28.8 MHz, the harmonic of its own crystal, and reads
  23 dB of S/N with nothing there.

## Bridge interface

Full specification: [docs/RB-SDR-RADIO-IF-v1.1.md](docs/RB-SDR-RADIO-IF-v1.1.md).

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
| `dual.hold_s` | 2.0 | how long a lock outlasts a transmission, for the reply |
| `dual.lookback_s` | 1.0 | how often it looks back at the primary from the secondary, once a transmission there ends |
| `dual.uninterrupted_s` | 30 | a transmission on the secondary longer than this, such as an ATIS, is looked past every `lookback_s` |
| `dual.hop_fail_limit` | 3 | failed hops in a row before it stays on the primary for `sdr.retry_s` |
| `demod.detector` | `envelope` | or `synchronous`, see below |
| `demod.noise_reduction_db` | 0 (off) | 0 to 20, how far hiss between words may be lowered |
| `audio.device` | empty (none) | ALSA device for `aplay -D`, from `aplay -L`. Empty: no audio output, no fault, recording carries on |
| `record.enabled`, `record.dir` | true, `/opt/stratux/www/playback` | WAV output |
| `record.max_total_mb`, `record.min_free_mb` | 128, 100 | hard limits on the folder |

### Audio on weak signals

A weak station is hissy because the carrier is barely above the noise, and no setting
changes that. The antenna and the dongle's gain do. These two options make the
hiss easier to live with. Try them first with `test-am --wav` and listen.

- **`demod.noise_reduction_db`** lowers the hiss between words by up to this many dB.
  It knows how loud the noise is from the same measurement as the squelch, so it does not
  mistake a continuous carrier such as ATIS for noise. In simulation, with a carrier 8 dB
  over the noise, the hiss in pauses fell from 5 to 12 dB under the speech at the 12 dB
  setting. It costs about 1 dB of clarity during speech and delays the audio by 32 ms.
  Start at 6; 12 is strong.
- **`demod.detector: synchronous`** locks onto the carrier and keeps only what is in phase
  with it. It sounds the same on a good signal, and is 1.7 dB better at the very edge
  (carrier level with the noise), where the envelope detector breaks up. With two
  carriers on the channel, as from an offset-carrier ground station, it follows one of
  them. Not yet tried on air.

Neither changes `sdrradiosnr` or the squelch.

### Squelch

`sdrradiosnr`, and the squelch with it, is the channel's **carrier** over the noise. An AM
transmitter sends its carrier for as long as it is keyed, between words too, and five
FFT bins hold it where the channel spreads its noise over hundreds. Noise alone reads 0,
however it is spread, so the squelch can sit just above it: 3 to 4 dB opens on a carrier
3 to 4 dB over the channel noise, and nothing else opens it. The channel's power was
used before the flight tests of 2026-11-10. Noise that was a little stronger inside the channel than beside
it (the tuner's shape, interference) read 2 to 3 dB with no station on, which is what
held a 3 dB squelch open while 4 dB missed calls.

An open squelch shuts `squelch.hysteresis_db` below its setting, but never lower than half
of it, after `squelch.hang_s`. A steady tone or a spur inside the channel looks like a
carrier and holds it open: `test-am --squelch 3` on an empty channel shows if one is there.

### Pops in the audio

The sound card and the dongle run from different clocks. When the card falls behind, a
block of audio is dropped; when it runs ahead, it plays a gap. Either is a pop, and the
log names it: `audio block dropped` or `audio underrun`. On dual watch the tuner no longer
leaves the secondary during a transmission, which was a pop every second.

## Living with the rest of the aircraft

**Dongle allocation is a matter of configuration.** Stratux gives each dongle tagged
`stratux:1090`, `stratux:978`, `stratux:868` or `stratux:162` to that service, then hands
any other dongle to an enabled service still without one. So give every enabled Stratux
service its own tagged dongle and turn off the services you do not use, and the radio's
dongle stays free. A dongle taken by mistake shows as `sdrradiofault` 2.

**ALSA is shared.** `audio.device` is empty as shipped, since a Raspberry Pi 5 has no
sound card. With one, `default` is the same as rbvoice's output. If that device is not
shared, the radio and voice alerts compete for it, and the loser
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
| AM voice through the daemon on RB-01 | hangar with antenna, 125.600, 2026-11-08: voice recorded under heavy hiss, the carrier about 5 dB over the noise | yes, weak signal |
| 2.7 kHz voice band, noise reduction, synchronous detector | simulated through the real chain; the filter and a noise reduction auditioned offline on those recordings | **no** |
| Spectral sense | R820T: normal on `test-fm` and on recorded airband spectra; a deliberately wrong `swap_iq` was caught. No inverted dongle has been seen | yes |
| Dual watch: hops, timing, lock and look back | R820T, a real carrier, indoors, and from the real RB-01 screen. No transmission confirmed | yes, no voice |
| Dual watch on a Pi, in a cockpit, by ear | not yet | **no** |
| VOR band: tuning | R820T, and the daemon driven from the RB-01 screen | yes |
| Gain and sensitivity in a cockpit | not yet | **no** |
| FC0012 | ladder, AGC and tuning read from the librtlsdr 2.0.2 source, stand-in, simulated zero-IF artefacts | **no** |
| CPU on a Raspberry Pi | not yet; 5 to 7 % of a core on x86. `python3 -m tools.cpu_cost` measures it | **no** |
| systemd unit, `install.sh` on a Pi | syntax-checked only | **no** |

## License

RB-Avionics is open source and open design. This repository is dual-licensed by XIAPROJECTS SRL:

1. **GNU Affero General Public License v3.0** (`AGPL-3.0-only`): the community edition, free for all
   builders and personal use. The full text is in [LICENSE](LICENSE). If you distribute this software or a
   modified version, or let others use a modified version over a network, you must offer them its
   complete source code under the same licence.
2. **Commercial licence** from XIAPROJECTS SRL, for products and services that cannot meet the
   AGPL-3.0 terms, for example closed-source devices, firmware or services. Ask through
   [www.rbavionics.com](https://www.rbavionics.com) or the [Discord community](https://discord.gg/G4u6kjmrUG).

Copyright © 2026 XIAPROJECTS SRL.
