"""The channel plan, checked against ICAO Annex 10 Vol V rather than the code."""

import math
import struct

import pytest

from rbsdrradio.freqrules import (
    BAND_MAX_KHZ,
    BAND_MIN_KHZ,
    SPACING_25,
    SPACING_833,
    all_channels,
    channel_from_name,
    parse_channel,
)

#: One 100 kHz block of the published table: name, frequency in MHz, spacing.
ICAO_TABLE = [
    (118.000, 118.0000, SPACING_25), (118.005, 118.0000, SPACING_833),
    (118.010, 118.0083, SPACING_833), (118.015, 118.0167, SPACING_833),
    (118.025, 118.0250, SPACING_25), (118.030, 118.0250, SPACING_833),
    (118.035, 118.0333, SPACING_833), (118.040, 118.0417, SPACING_833),
    (118.050, 118.0500, SPACING_25), (118.055, 118.0500, SPACING_833),
    (118.060, 118.0583, SPACING_833), (118.065, 118.0667, SPACING_833),
    (118.075, 118.0750, SPACING_25), (118.080, 118.0750, SPACING_833),
    (118.085, 118.0833, SPACING_833), (118.090, 118.0917, SPACING_833),
]


@pytest.mark.parametrize("name,mhz,spacing", ICAO_TABLE)
def test_published_table(name, mhz, spacing):
    channel, reason = parse_channel(name)
    assert channel is not None, reason
    assert channel.freq_hz == pytest.approx(mhz * 1e6, abs=50)
    assert channel.spacing == spacing


@pytest.mark.parametrize("last_digits", [20, 45, 70, 95])
def test_names_that_are_not_channels(last_digits):
    channel, reason = parse_channel((118_000 + last_digits) / 1000)
    assert channel is None
    assert "not a 25 or 8.33 kHz channel" in reason


def test_band_holds_760_25khz_and_2280_833khz_channels():
    """19 MHz of band: the two plans' channel counts are published figures."""
    channels = all_channels()
    assert sum(c.spacing == SPACING_25 for c in channels) == 760
    assert sum(c.spacing == SPACING_833 for c in channels) == 2280


def test_every_frequency_is_on_the_833_grid_and_inside_the_band():
    step = 25_000 / 3
    for c in all_channels():
        assert 118_000_000 <= c.freq_hz < 137_000_000
        grid = (c.freq_hz - 118_000_000) / step
        assert abs(grid - round(grid)) * step <= 1.0, c


@pytest.mark.parametrize("mhz,expected", [(118.0, 118_000), (136.975, 136_975),
                                          (136.99, 136_990)])
def test_band_edges_accepted(mhz, expected):
    assert parse_channel(mhz)[0].name_khz == expected


@pytest.mark.parametrize("mhz", [117.975, 117.0, 137.0, 137.025, 108.0, 0.1, 1090.0])
def test_out_of_band_rejected(mhz):
    channel, reason = parse_channel(mhz)
    assert channel is None
    assert "outside the COM band" in reason


def test_every_channel_survives_the_float32_bridge():
    """RB-01 stores values as float32. Each name must come back as the same channel."""
    for c in all_channels():
        through_bridge = struct.unpack("<f", struct.pack("<f", c.name_khz / 1000))[0]
        assert parse_channel(through_bridge)[0] == c


def test_a_raw_frequency_is_not_mistaken_for_a_name():
    """118.0083 is what 118.010 tunes, not a name. Rounding it would hide a UI bug."""
    assert parse_channel(118.0083)[0] is None


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, True, "118.7", None])
def test_junk_is_rejected_without_raising(value):
    assert parse_channel(value)[0] is None


def test_labels():
    assert channel_from_name(118_030).label == "118.030"
    assert channel_from_name(BAND_MIN_KHZ).label == "118.000"
    assert channel_from_name(BAND_MAX_KHZ).label == "136.990"
