# Tech debt

Known gaps, deferred on purpose. Each entry says what is missing, what happens meanwhile,
and when it is finished.

| # | Item | Owner | Status |
|---|---|---|---|
| TD1 | RB-01 does not list radio recordings | RB-01 radio screen | open |
| TD2 | Radio keep-alive: UI and protocol for all supported devices | RB-Avionics suite | open |

---

## TD1: RB-01 does not list radio recordings

Opened 2026-11-05.

**What is missing.** The daemon writes `radio_YYYYMMDD_HHMMSS_<hertz>.wav` to
`/opt/stratux/www/playback`, and `/playback` returns those files, but the playback list in
`web/RB-01/plates/js/radio.js` keeps only names starting `vrin`:

```js
if(list[reverseIndex].Name.startsWith("vrin")){
```

**Meanwhile.** Recording works and the files are kept. The folder only stores them; the
list will show `radio_{information}`. Nothing needs doing in this repository.

**One trap when fixing it.** Widening the prefix test alone is not enough. `/playback`
returns names in alphabetical order, and the loop walks from the end and stops at 20
entries. `vrin*` sorts after `radio_*`, so once 20 `vrin` files exist no radio recording
would ever be reached. Sort by `ModTime` before taking the newest 20.

**Already compatible.** The name has the four `_` fields the existing parser expects, so
the frequency column works, and `Size / 30000` reads about 6 % long for these WAV files.

**Finished when** a new transmission appears in the RB-01 list with its channel, and plays.

---

## TD2: Radio keep-alive, UI and protocol for all supported devices

Opened 2026-11-05.

**What is missing.** No radio tells the screen that it is still alive, so the screen cannot
tell a quiet radio from a dead one.

- RB-01 keeps the last bridge values for ever, and rebroadcasts only values that changed.
- A device repeating a steady payload therefore refreshes the map but reaches no client.
- A device that loses power leaves its last status on the bridge: for this daemon, possibly
  `sdrradiostatus` 4, Receiving, indefinitely.

**Meanwhile.** A clean stop publishes status 0. The daemon repeats its status every
`publish.keepalive_s`, which rebuilds RB-01's map after RB-01 restarts but gives clients no
signal. A power cut goes unnoticed on the screen. Consumers have no way to detect it today,
and the interface document says so.

**Scope.** One design for every radio RB-01 supports, not a fix per device:

- SDR radios on the bridge: this daemon, and rb-vor, which has the same gap with `vorstatus`
- the `SERIAL` and `NMEA` radio drivers in `main/radio.go`
- how the RB-01 radio screen shows a radio that stopped answering

**To decide.**

- Where liveness lives: in RB-01's bridge, for example the age of each key or device, or in
  a field every device publishes.
- The timeout, and whether it differs between bridge and RS-232 devices.
- What the UI shows, and whether the last values stay visible.

**Do not** add a heartbeat to one device on its own. `sdrradioheartbeat` was built for this
daemon and removed on 2026-11-05 for that reason: a mechanism on a single websocket makes
no sense until the protocol is settled for all of them.

**Finished when** every supported radio reports liveness the same way, the RB-01 screen
shows a lost radio, and section 3.3 of the interface document describes it.
