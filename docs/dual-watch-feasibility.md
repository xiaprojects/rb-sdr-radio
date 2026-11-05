# Dual watch on one RTL-SDR: feasibility

**Status:** study only, nothing is implemented. Measured on 2026-11-05 on one R820T (Nooelec
NESDR Nano 3), indoors, on an x86 PC. No FC0012 was available, so its figures come from the
librtlsdr 2.0.2 source and are labelled as estimates.

**Question.** Can one dongle watch a primary and a secondary channel, stay silent until
either carries a signal, then lock on that channel and play it?

Figures are measured unless marked **Source** (read from librtlsdr), **Estimate** or **Model**
(a simulation with assumed traffic).

---

## 1. Answer

**Yes, by cycling the tuner between the two channels. On the R820T the hopping held up on the
bench, though not yet in an aircraft, and cycling always costs the primary something.** One
tuner hears one place at a time, so this is watching in turns, not receiving both at once. A
production aircraft radio does the same: the Trig TY91 "will scan between the active and
standby frequencies", and "a transmission on the primary channel will interrupt the secondary
channel".

| | R820T | FC0012 |
|---|---|---|
| Feasible | **Yes, measured** | Probably, not measured |
| Blind time per hop | 32.5 ms | 40 to 55 ms, **Estimate**, plus any settling |
| Full cycle, primary to secondary and back | 97 ms | 110 to 140 ms, **Estimate** |
| Hops that failed to land | None confirmed, 1 unexplained in 11,414 | Unknown |
| Tells you when the tuner failed to lock | Only on stderr | Never |

Three findings decide the design:

1. **Cycling works because transmissions last seconds.** The tuner is blind for 67 % of the
   time and listens to each channel for only 16 ms in 97, yet a transmission in progress is
   found within one cycle. The price is its start: 30 to 125 ms when scanning.
2. **Reliability comes from the priority rule, not the tuner.** While locked on the
   secondary, the radio must keep looking back at the primary. Without that, the model loses
   8 to 9 % of primary transmissions in normal traffic, and 94 % if the secondary is an ATIS,
   which never stops. Looking back every second brings this to about 1 % or less.
3. **Hop speed matters less than expected.** Going from a 97 ms cycle to 257 ms barely
   changes how many transmissions are lost. It changes how much of each start is clipped.

**Recommendation.**

- Build cycling as an optional mode. With no secondary set, nothing changes from today.
- Ship it on the R820T first. Measure an FC0012 (section 8) before promising it there.
- Where nothing may be lost, fit a second dongle. Cycling cannot match that.

---

## 2. Three ways to get two channels

| | Cycling (this study) | One wide capture | Second dongle |
|---|---|---|---|
| Both heard at the same moment | No | Yes | Yes |
| Channel separation | Anywhere in the band | Within the passband only | Anywhere in the band |
| What is lost | Starts of transmissions, sometimes more | Nothing | Nothing |
| CPU | No more than today | 2 to 5 times today, not measured on a Pi | About 2 times today |
| Hardware | None | None | Dongle, plus splitter (about 3.5 dB) or antenna |

**One wide capture** tunes between the two channels and filters both out of the same samples.
Nothing is blind, but both must fit in the passband: about 0.8 MHz at today's 1.024 Msps
(the response is already falling at +470 kHz here), and about 2 MHz at 2.4 Msps, an
**Estimate**. Two channels picked at random from the 19 MHz band fit about 1 time in 12, or 1
in 5 at 2.4 Msps, so this cannot be the general answer. It also needs a mixer per channel in place of today's cheap
fs/4 mixer. On a zero-IF FC0012 the channels must not sit either side of the tuner at equal
distance, because each would land on the other's mirror image.

The rest of this document is about cycling.

---

## 3. How the cycle works

### 3.1 One hop

1. `rtlsdr_set_center_freq(channel - 256 kHz)`, the same fs/4 offset as today.
2. `rtlsdr_reset_buffer`. Without it the first 2.0 ms of samples are from the old channel,
   and a strong signal there could leak into the reading. A longer fill would also work, and
   saves under 2 ms.
3. Reset the demodulator and feed it 3 ms, which fills the filters. Ignore the result.
4. Feed it 13 ms and read the S/N.
5. If S/N reaches the squelch, stay 16 ms more to confirm, then lock. Otherwise hop.

```
                          one cycle, 97 ms
|<------------------------------------------------------------------->|
|  hop to primary |fill|  measure |  hop to secondary |fill|  measure  |
|     32.5 ms     | 3  |    13    |     32.5 ms       | 3  |    13     |
     blind                              blind
```

Audio stays silent all through this. Nothing plays until a lock.

### 3.2 Locking

| State | Tuner | Heard | Leaves when |
|---|---|---|---|
| Scan | Cycling | Silence | Signal confirmed: lock on that channel |
| Locked, primary | Primary | Primary | Squelch shut for `hold_s`: scan |
| Locked, secondary | Secondary, looking back | Secondary | Primary found: lock on it. Squelch shut for `hold_s`: scan |

The rules:

- **The primary always wins.** It is never left while locked, and the secondary is not
  checked until it has been quiet for `hold_s`.
- **Look back from the secondary every `lookback_s`,** during the hold as well. Each look
  costs 81 ms of secondary audio: two hops and one dwell.
- **Hold after a transmission,** because the reply usually follows on the same channel.
- **Confirm before locking.** One reading is not enough, for the reason `Squelch` already
  counts two: ignition noise is impulsive and a carrier is not. It adds 16 ms, only when a
  signal is there.
- **After any lock, look at the primary first.**

| Starting value | | Why |
|---|---|---|
| `hold_s` | 2.0 | Equals `record.hold_s`; RTLSDR-Airband also waits about 2 s |
| `lookback_s` | 1.0 | See section 7 |
| Dwell | 3 + 13 ms | As good as 64 ms once a signal is 2 dB over the squelch (section 6) |
| `sdr.gain_mode` | `manual` | One gain serves both channels. AGC would restart after each hop, not measured |

Three special cases:

- Secondary 0: no cycling, exactly today's behaviour.
- Squelch 0, the "squelch test": every dwell would "find a signal", so cycling stops and the
  radio stays on the primary.
- Repeated hop errors: drop to primary only, raise a fault, and retry later.

### 3.3 Time budget

| Step, ms | R820T **Measured** | FC0012 **Estimate** | Today's chain, only hopped |
|---|---|---|---|
| `rtlsdr_set_center_freq` | 28.6 | 35 to 50 | 28.6 |
| `rtlsdr_reset_buffer` | 3.9 | 3.9 | 3.9 |
| Extra settling | 0 | Unknown | 0 |
| Fill and measure | 16 | 16 | 96 |
| **Per channel** | **48.5** | 55 to 70 | 128.5 |
| **Full cycle** | **97** | 110 to 140 | 257 |

Today's chain needs 96 ms because the first 32 ms block reads 0 and `Squelch` wants two more.
**Measured** on the real code. With `sdr.settle_ms` at 100 on top, the cycle would be 457 ms.

These are the radio's own times. The soak ran at 105 ms because its analysis shared the
thread, and a daemon should expect about that on a PC, and more on a Pi.

---

## 4. R820T

**Low IF, one ADC.** The R820T hands the RTL2832U a real signal at an intermediate frequency
and the chip mixes it down digitally, so there is no I/Q image and no analogue DC offset to
disturb after a hop. **Source.** At 1.024 Msps the IF filter is 1.2 MHz and the LO sits
1.7 MHz above the tuned frequency.

**A hop is 11 USB control transfers. Source.**

| Transfers | What |
|---|---|
| 2 | Open the I2C repeater |
| 1 | PLL autotune fast, 128 kHz |
| 2 | Read VCO fine tune |
| 1 | Write seven PLL registers |
| 2 | Read the lock bit |
| 1 | PLL autotune slow, 8 kHz |
| 2 | Close the I2C repeater |

- Unchanged registers are skipped by a shadow copy, so the transfer count does not grow
  with the distance hopped. Channels below about 118.556 MHz use a different tracking-filter
  entry, and hopping across it added no more than the normal variation between pairs.
- **A failed lock is reported as success.** If the lock bit stays clear after one retry at
  higher VCO current, `r82xx_set_pll` prints `[R82XX] PLL not locked!` and returns 0, so
  `rtlsdr_set_center_freq` returns 0. Nothing in the API shows it. The same line appears once
  at start-up and means nothing there: `rtlsdr_set_sample_rate` retunes before any frequency
  is set.

---

## 5. FC0012

**Zero IF, two ADCs.** The tuner delivers I and Q directly, and the RTL2832U runs its DC
cancellation and I/Q compensation on them (register `0xb1` set to `0x1b`). **Source.** This is
the main unknown: those loops may need time to recover after each hop, and R820T timing says
nothing about that. The 256 kHz offset keeps the DC step out of the channel filter, but
whether the I/Q loop disturbs the first milliseconds has to be measured.

**A hop is 17 to 20 USB control transfers. Source.**

| Transfers | What |
|---|---|
| 4 | Open and close the I2C repeater |
| 2 | Read and write the VHF/UHF GPIO |
| 6 | Write the synthesiser registers, always, with no shadow copy |
| 3 | VCO calibration |
| 2 | Read the VCO voltage back |
| 0 or 3 | Calibrate again if that voltage is out of range |

The estimate in section 3.3 scales from the R820T: 2.0 ms per transfer for `reset_buffer`,
2.6 ms per transfer for a whole R820T hop, so 34 to 52 ms for 17 to 20 transfers.

- **No lock detect exists.** The driver writes the PLL and reads only a VCO voltage, and
  librtlsdr has commented out the `msleep(10)` before that read, so it may see an unsettled
  value. `rtlsdr_set_center_freq` reports only whether the I2C writes succeeded.
- **Channels at or above about 127.756 MHz use the upper VCO range** (`f_vco >= 3.06 GHz`,
  tuned frequency times 24). A pair straddling it changes range at every hop. Test that pair
  as well as a pair on one side.
- **Manual gain only.** `fc0012_set_gain_mode` is an empty stub, so `sdr.py` forces manual
  gain, at most 19.2 dB in the shipped profile.
- **Sensitivity may hurt most.** Cycling adds no noise, but a signal must clear the squelch
  within a dwell. rb-vor A28 rates the FC0012 a poor VHF receiver, which would push more
  transmissions down near the threshold, where section 6 shows detection falls away. Its
  claim has not been measured here.

---

## 6. Measurements

**Setup.** R820T, 1.024 Msps, manual gain 28 dB, indoors, PC USB 2.0. Test signals were two
steady carriers at this antenna, 124.4155 and 120.000 MHz, placed at the +256 kHz position.
Their origin is unknown, but neither is a baseband spur: each leaves +256 kHz when the tuner
moves away. They are weak, 2 to 6 dB S/N in the real chain, so they prove timing, not
reception. This site has a 299.75 Hz impulse comb at the antenna input, so the noise tests
below include real impulses, though not an engine's.

**Hop test, 1,213 hops.** Spans of 0.6, 4.4, 6.3 and 12.5 MHz, idle and while streaming.

| Result | Value |
|---|---|
| `rtlsdr_set_center_freq` | Median 25.6 to 28.9 ms by pair, worst 31.6 ms |
| Failed calls, failed reads, short reads | 0 |
| `PLL not locked` during hopping | 0 |
| Old-channel samples after the call | 2.0 ms, or 4,096 bytes, the same as `BUFFER_DUMP` in `rtl_fm` |
| After `rtlsdr_reset_buffer` | Correct level from the first 0.5 ms |
| Extra settling after the call | None. The change happens roughly 20 ms into the call, inside the blind time |
| Carrier present | 280 of 280 stored hops onto a carrier channel |
| Frequency after a hop | First 8 ms the same as 64 to 90 ms later, within the 8 Hz resolution of the test |
| Total power through a hop | Flat, no transient |
| The 3 + 13 ms dwell straight after a hop, quiet channel | All 460 readings 0 dB, through the real chain with both filters |
| The same dwell on a carrier, against one 60 ms later | Within 0.2 dB, so a hop leaves nothing behind |

**Soak, 10 minutes at a 105 ms cycle.** 96 ms of that is the radio, the rest the bench's own
analysis. Both channels carried a carrier, so every hop could be checked.

| Result | Value |
|---|---|
| Hops | 11,414 |
| Failed calls, failed reads, short reads | 0 |
| `PLL not locked` during hopping | 0 |
| 120.000 MHz | Carrier on 5,707 of 5,707 hops, never under 19 dB over the noise, always within one 62.5 Hz bin |
| 124.4155 MHz | Carrier on 5,706 of 5,707 hops, 1 unexplained |
| `rtlsdr_set_center_freq` | Median 28.2 ms, 99.9 % under 33.6 ms, worst 40.3 ms, no trend over the run |
| Whole hop and 16 ms dwell | Median 48.0 ms, worst 60.5 ms |

**The one unexplained dwell.** 124.4155 MHz fades by itself: each reading correlates 0.90 with
the next, and its frequency wanders while 120.000 MHz holds still. That accounts for 31 of the
32 dwells under 12 dB, which come in runs at the carrier's usual frequency. The last, at
26.8 s, stands alone: no carrier visible, with 19 and 16 dB either side, a normal 27.7 ms call
and no `PLL not locked`. Its samples were not kept, so it may be a brief fade or a hop that did
not land, and it counts as a possible failure. That is 1 in 11,414, at worst about 1 in 2,400
(95 %). It would cost one dwell: the next hop retunes, and the carrier was back on the next look.

**Detection in one dwell.** Real noise from a 200 s capture plus synthetic AM of known S/N,
through the real `AmDemod`, 25 kHz channel, squelch 10 dB, 250 trials per cell.

| True S/N | 8 ms dwell | 16 ms dwell | 64 ms dwell |
|---|---|---|---|
| 8 dB | 12 % | 4 % | 1 % |
| 10 dB | 90 % | 96 % | 98 % |
| 12 dB and above | 100 % | 100 % | 100 % |
| Noise only | 0 of 250 | 0 of 250 | 0 of 250 |

So a dwell needs a signal about 2 dB over the squelch. Below that, a transmission is found
only by chance. An 8.33 kHz channel behaves the same, reading about 2 dB lower here, where its
noise level was only approximated.

**Real traffic replay.** The hop schedule applied to the same capture on 124.200 MHz, with
the squelch lowered to 6 dB because signals indoors barely reach 10 dB on the 25 kHz filter.
It held only two transmissions above 6 dB, each tried on six schedule phases, so treat this as
a sanity check.

| Dwell | Cycle | Cases caught | Start lost, median / p95 | Fires outside a transmission |
|---|---|---|---|---|
| 8 ms | 81 ms | 8 of 12 | 34 / 66 ms | 5 in 14,808 dwells |
| 16 ms | 97 ms | 9 of 12 | 44 / 88 ms | 1 in 12,365 dwells |
| 64 ms | 193 ms | 8 of 12 | 76 / 177 ms | 0 in 6,215 dwells |

- The 3.5 s transmission, peak 10.3 dB, was caught on all six phases at 16 ms. All the misses
  were the 1.3 s one, peak 7.2 dB, barely over the threshold.
- The one 16 ms fire was real signal just under the threshold, not noise: a continuous
  receiver read 3.8 to 6.3 dB around it.

**CPU.** A dwell costs about 1.8 ms on this PC, about 3.7 % of a core at 20.6 dwells per
second, against 5.5 % for continuous reception in the same test.

**What this does not show.** One dongle, one room, one session. No engine, no Pi, no
real-time audio, no controller: the soak is a bench loop, not the daemon.

---

## 7. Reliability

### 7.1 What a pilot would get

**Model.** Six simulated hours at the measured R820T timing, without the 16 ms confirm.
Transmissions have a median of 3 s (lognormal, sigma 0.7) with random gaps, and `hold_s` is 2.
The traffic is assumed, not measured. "Lost" means no part of the transmission was heard, and
a real radio on the primary alone would score 0 %.

| Airtime, primary / secondary | Look back | Primary lost | Primary start clipped, median / p95 | Secondary audio cut |
|---|---|---|---|---|
| Light, 10 % / 10 % | Never | 8.0 % | 59 / 203 ms | 0 |
| | 2 s | 0.9 % | 61 / 964 ms | 5 % |
| | **1 s** | **0.4 %** | **61 / 593 ms** | **10 %** |
| Busy, 30 % / 15 % | Never | 9.2 % | 52 / 1,595 ms | 0 |
| | 2 s | 1.8 % | 55 / 1,255 ms | 5 % |
| | **1 s** | **0.3 %** | **55 / 712 ms** | **10 %** |
| ATIS as secondary, 30 % / continuous | Never | 93.6 % | 78 / 3,459 ms | 0 |
| | 2 s | 7.8 % | 688 / 1,947 ms | 4 % |
| | **1 s** | **1.1 %** | **388 / 1,015 ms** | **8 %** |

- **One-second look-back is the right default.** It is the only row that holds up when the
  secondary is an ATIS, which is exactly the use the Trig manual gives for its monitor.
- **The clipped start has two sizes.** From scan, 30 to 125 ms (about 75 ms on average) by
  the time budget, confirm included. That is less than a syllable, a judgement to check by
  ear. If the primary starts during a secondary lock, up to about 1.1 s.
- **The secondary pays.** In busy traffic it is heard only about 54 % of the time, and about
  a quarter of its transmissions are lost entirely, because the primary and its hold come
  first. A secondary here is a courtesy, not a watch.
- **Hop speed changes the clip, not the losses.** Busy traffic, 2 s look-back: 1.8 % lost and
  55 ms clipped on the R820T, 1.8 % and 73 ms on the FC0012 estimate, 2.1 % and 177 ms on
  today's chain.

### 7.2 What can go wrong

| Risk | Effect | Answer |
|---|---|---|
| Secondary never stops: ATIS, VOLMET, stuck microphone | Primary shut out | Look-back. Mandatory, not optional |
| Primary never stops | Secondary never heard | By design. Show it on the screen |
| Signal near the squelch | Found late, or a short call missed | Inherent. Keep 2 dB of margin in mind when setting the squelch |
| Tuner fails to lock, silently | That channel looks empty for one dwell | The next hop retunes anyway, so scanning repairs itself |
| Ignition or other impulse | False lock for `hold_s` | The confirm reading. Not measured with an engine |
| USB error on a hop | Call returns an error | Primary only, fault shown, retry |
| Slow USB on a Pi, shared with Stratux dongles | Longer cycle, more clip, no failure | Measure it |
| Strong and weak channel share one gain | Overload, as today | Set for the primary |
| Register wear | None: hops write registers, not the EEPROM | Nothing needed |

---

## 8. Before calling it reliable

| # | Measure | Pass |
|---|---|---|
| 1 | FC0012 on the bench: the same hop test, pairs on one side of 127.756 MHz and across it | Hop and settling 60 ms or less; carrier found on 10,000 of 10,000 hops |
| 2 | FC0012: first 20 ms after a hop through the real chain, on an empty channel | No S/N reading above 3 dB |
| 3 | R820T soak for 2 hours, about 150,000 hops, on two stable carriers, keeping the samples of any dropout | Every dropout explained, and any from the tuner repaired by the next hop |
| 4 | Raspberry Pi, with the Stratux dongles running | Cycle 150 ms or less, no overruns |
| 5 | Cockpit, engine running, 30 minutes on quiet channels | No false locks |
| 6 | Listening test on real traffic | Clipped starts and the 81 ms cuts judged acceptable |

Items 1 and 2 decide the FC0012. A dongle with the tuner and a steady carrier on two channels
is all it takes, about 10 minutes.

---

## 9. What it would change

Not started. This is the size of the work, to be planned separately.

| Where | Change |
|---|---|
| `sdr.py` | Hop inside `SdrReader._run`, between reads, from a one-slot request, and tag each block with its channel. Today `Controller._tune` retunes on the event loop, which 20 hops a second at 29 ms each would stall, against the rule that nothing blocks the sample path |
| `sdr.py` | Blocks of about 8 ms while scanning, so a dwell can end on time |
| `dsp.py` | `AmDemod` takes a short meter: `nfft` 2048 gives a noise floor in 16 ms, where 4096 needs 32 ms |
| `controller.py` | Scan and lock states, still synchronous and clock-injected, with `Squelch` used as it is once locked |
| `controller.py` | Write silence for the blind time, or `aplay` underruns during scanning |
| `recorder.py` | None. `feed` already ends a segment when the channel changes |
| `publisher.py` | Hops must never reach the bridge: each change is rebroadcast to every client, and 20 a second would flood them. `sdrradiostatus` stays 3 while scanning and 4 while locked |
| Interface | Additive keys for v1.1: a secondary-frequency command, its active value, and which channel is locked. `sdrradiofreqactive` keeps showing the primary. Changing `publisher.FIELDS` needs the interface document changed too |

---

## Sources

- librtlsdr 2.0.2: `librtlsdr.c`, `tuner_r82xx.c`, `tuner_fc0012.c`
- rb-vor `CLAUDE.md`, A28: FC0012 fitted on the aircraft and rated as a VHF receiver
- [Trig TY91 and TY92 VHF Radio Operating Manual, 00840-00-AE](https://trig-avionics.com/library/00840-00-AE%20TY91%20and%20TY92%20VHF%20Radio%20Operating%20Manual%20-%20PDF%20-%20March%202022.pdf), Monitor
- [RTLSDR-Airband, scan mode](https://github.com/charlie-foxtrot/RTLSDR-Airband/wiki/Configuring-channels-for-scan-mode): 5 frequencies per second, and [`rtl_airband.cpp`](https://github.com/rtl-airband/RTLSDR-Airband/blob/main/src/rtl_airband.cpp), `controller_thread`: about 2 s before moving on
- [`rtl_fm.c`](https://github.com/osmocom/rtl-sdr/blob/master/src/rtl_fm.c): `BUFFER_DUMP` and "hacky hopping"
