"""VHF COM channel rules: ICAO 25 kHz and 8.33 kHz channel names.

What a pilot dials is a channel *name*, not a frequency. On the 8.33 kHz plan
(ICAO Annex 10 Vol V, table 4-1 bis) the two differ, and the name also carries
the spacing, which decides the receive filter:

=======  ===========  =======
name     tunes        spacing
=======  ===========  =======
118.000  118.0000     25
118.005  118.0000     8.33
118.010  118.0083     8.33
118.015  118.0167     8.33
118.025  118.0250     25
118.030  118.0250     8.33
=======  ===========  =======

Treating `118.030` as 118.030 MHz would put the receiver 5 kHz off the carrier,
which is outside the 8.33 kHz filter. Names ending 20, 45, 70 and 95 are not
channels at all.

All arithmetic is in integer kHz and Hz: bridge values arrive as float32, so a
frequency must never be compared as a float (rb-vor, freqrules).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = ["Channel", "parse_channel", "channel_from_name", "all_channels",
           "BAND_MIN_KHZ", "BAND_MAX_KHZ", "SPACING_25", "SPACING_833"]

BAND_MIN_KHZ = 118_000
#: 136.990 tunes 136.99167 MHz, the last channel below the 137 MHz band edge.
BAND_MAX_KHZ = 136_990

SPACING_25 = "25"
SPACING_833 = "8.33"

#: Last two digits of the name -> (steps of 25/3 kHz above the 100 kHz boundary,
#: spacing). The table, not a formula: it is the published plan.
_DIGITS = {
    0: (0, SPACING_25), 5: (0, SPACING_833), 10: (1, SPACING_833), 15: (2, SPACING_833),
    25: (3, SPACING_25), 30: (3, SPACING_833), 35: (4, SPACING_833), 40: (5, SPACING_833),
    50: (6, SPACING_25), 55: (6, SPACING_833), 60: (7, SPACING_833), 65: (8, SPACING_833),
    75: (9, SPACING_25), 80: (9, SPACING_833), 85: (10, SPACING_833), 90: (11, SPACING_833),
}

#: How far a value may sit from a whole kHz and still be that name. Generous
#: enough for float32 (about 8 Hz at 136 MHz), far too tight to accept a raw
#: frequency such as 118.0083 as if it were the name 118.010.
_NAME_TOLERANCE_KHZ = 0.05


@dataclass(frozen=True)
class Channel:
    name_khz: int
    freq_hz: int
    spacing: str

    @property
    def label(self) -> str:
        return f"{self.name_khz / 1000:.3f}"


def channel_from_name(name_khz: int) -> Channel | None:
    """The channel with this name in kHz, or None if the name does not exist."""
    if not BAND_MIN_KHZ <= name_khz <= BAND_MAX_KHZ:
        return None
    entry = _DIGITS.get(name_khz % 100)
    if entry is None:
        return None
    steps, spacing = entry
    base_hz = (name_khz - name_khz % 100) * 1000
    return Channel(name_khz, base_hz + (steps * 25_000 + 1) // 3, spacing)


def parse_channel(mhz: float) -> tuple[Channel | None, str]:
    """`(channel, "")`, or `(None, reason)` when `mhz` is not a COM channel name."""
    if not isinstance(mhz, (int, float)) or isinstance(mhz, bool) or not math.isfinite(mhz):
        return None, f"not a number: {mhz!r}"
    khz = mhz * 1000.0
    name = round(khz)
    if not BAND_MIN_KHZ <= name <= BAND_MAX_KHZ:
        return None, f"{mhz:.3f} MHz is outside the COM band 118.000-136.990"
    channel = channel_from_name(name) if abs(khz - name) <= _NAME_TOLERANCE_KHZ else None
    if channel is None:
        return None, f"{mhz:.4f} is not a 25 or 8.33 kHz channel name"
    return channel, ""


def all_channels() -> list[Channel]:
    """Every channel in the band, in name order."""
    found = (channel_from_name(k) for k in range(BAND_MIN_KHZ, BAND_MAX_KHZ + 1, 5))
    return [c for c in found if c is not None]
