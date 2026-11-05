"""Dongle selection, the reader thread, and the C binding.

The binding tests compile a fake librtlsdr against the real header, so a wrong
signature or a wrong call order shows up here and not first in an aircraft.
"""

import ctypes.util
import shutil
import subprocess
import threading
from pathlib import Path

import numpy as np
import pytest
from conftest import wait_until

from rbsdrradio import sdr
from rbsdrradio.__main__ import main
from rbsdrradio.dsp import FS_HZ, TUNER_OFFSET_HZ
from rbsdrradio.sdr import (
    FakeDevice,
    RtlDevice,
    SdrError,
    SdrReader,
    apply_tuner_profile,
    resolve_device,
    snap_gain,
)

FC0012_LADDER = [-9.9, -4.0, 7.1, 17.9, 19.2]
R820T_LADDER = [0.0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4, 15.7, 16.6, 19.7, 20.7, 22.9,
                25.4, 28.0, 29.7, 32.8, 33.8, 36.4, 37.2, 38.6, 40.2, 42.1, 43.4, 43.9, 44.5,
                48.0, 49.6]                       # read from a real R820T by rtl_test
FC0012, R820T = "2", "5"                          # librtlsdr's enum rtlsdr_tuner


# -- selection policy --------------------------------------------------------- #

def test_selects_by_serial_whatever_the_index():
    assert resolve_device({"device_serial": "radio"}, ["a", "b", "radio"]).index == 2


def test_serial_beats_index():
    choice = resolve_device({"device_serial": "radio", "device_index": 0}, ["a", "radio"])
    assert choice.index == 1


def test_wrong_serial_never_falls_back_and_names_what_is_there():
    with pytest.raises(SdrError) as e:
        resolve_device({"device_serial": "radio", "device_index": 0}, ["stratux:1090", ""])
    assert "stratux:1090" in str(e.value) and "<no serial>" in str(e.value)


def test_duplicate_serials_are_an_error():
    with pytest.raises(SdrError, match="share serial"):
        resolve_device({"device_serial": "x"}, ["x", "x"])


def test_selects_by_index():
    choice = resolve_device({"device_serial": "", "device_index": 1}, ["a", "b"])
    assert (choice.index, choice.serial) == (1, "b")


def test_index_out_of_range():
    with pytest.raises(SdrError, match="valid 0..0"):
        resolve_device({"device_serial": "", "device_index": 3}, ["a"])


def test_no_dongle():
    with pytest.raises(SdrError, match="no RTL-SDR"):
        resolve_device({"device_serial": "", "device_index": 0}, [])


def test_warns_when_several_dongles_and_none_pinned():
    assert "pin this dongle" in resolve_device({"device_index": 0}, ["a", "b"]).warning
    assert resolve_device({"device_index": 0}, ["a"]).warning == ""


# -- tuner quirks ------------------------------------------------------------- #

def test_gain_snaps_to_the_fc0012_ladder_and_says_so():
    gain, note = snap_gain(32.8, FC0012_LADDER)
    assert gain == 19.2 and "32.8" in note


def test_gain_on_a_step_is_silent():
    assert snap_gain(19.2, FC0012_LADDER) == (19.2, "")


def test_shipped_profiles_name_real_steps_and_do_not_guess_the_sense(cfg):
    profiles = cfg.sdr["tuner_profiles"]
    assert snap_gain(profiles["FC0012"]["gain_db"], FC0012_LADDER)[1] == ""
    assert snap_gain(profiles["R820T"]["gain_db"], R820T_LADDER)[1] == ""
    assert "swap_iq" not in profiles["FC0012"]    # a measurement, not an assumption (rb-vor O21)


def test_profile_overrides_only_its_own_tuner():
    base = {"gain_db": 32.8, "ppm": 0, "tuner_profiles": {"FC0012": {"gain_db": 19.2}}}
    assert apply_tuner_profile(base, "FC0012")[0]["gain_db"] == 19.2
    assert apply_tuner_profile(base, "R820T")[0]["gain_db"] == 32.8


# -- reader thread ------------------------------------------------------------ #

def reader_config(**over):
    return {"block_samples": 1024, "buffer_blocks": 4, "swap_iq": False, **over}


def test_applies_the_tuner_offset(clock):
    device = FakeDevice(lambda n: np.zeros(n, dtype=np.complex64))
    reader = SdrReader(reader_config(), opener=lambda cfg: device)
    reader.set_frequency(118_700_000)
    assert device.freq_hz == 118_700_000 + TUNER_OFFSET_HZ
    reader.close()


def test_close_releases_and_a_retune_reopens():
    opened = []

    def opener(cfg):
        opened.append(FakeDevice(lambda n: np.zeros(n, dtype=np.complex64)))
        return opened[-1]

    reader = SdrReader(reader_config(), opener=opener)
    reader.set_frequency(118_700_000)
    assert reader.is_open
    reader.close()
    assert not reader.is_open and opened[0].closed
    reader.close()                                 # idempotent
    reader.set_frequency(121_500_000)
    assert len(opened) == 2 and reader.is_open
    reader.close()


def test_a_full_queue_drops_the_oldest_and_flags_it():
    counter = iter(range(10 ** 6))
    gate = threading.Event()

    def source(n):
        value = next(counter)
        if value == 20:
            gate.set()
        return np.full(n, value, dtype=np.complex64)

    reader = SdrReader(reader_config(), opener=lambda cfg: FakeDevice(source))
    reader.set_frequency(118_700_000)
    assert gate.wait(5.0)
    assert wait_until(lambda: reader.overruns >= 5)
    reader.close()
    assert reader.overruns >= 5


def test_blocks_returns_even_if_the_reader_outruns_the_consumer():
    """rb-vor F5: an unbounded drain would starve the event loop."""
    reader = SdrReader(reader_config(), opener=lambda c: FakeDevice(
        lambda n: np.zeros(n, dtype=np.complex64)))
    reader.set_frequency(118_700_000)
    wait_until(lambda: reader.blocks_read > 8)
    assert len(list(reader.blocks())) <= reader.cfg["buffer_blocks"]
    reader.close()


def test_retune_discards_what_was_in_flight():
    device = FakeDevice(lambda n: np.zeros(n, dtype=np.complex64))
    reader = SdrReader(reader_config(), opener=lambda c: device)
    reader.set_frequency(118_700_000)
    wait_until(lambda: reader.blocks_read > 2)
    reader.set_frequency(121_500_000)
    reader.close()
    assert reader._q.qsize() == 0


def test_swap_iq_conjugates():
    device = FakeDevice(lambda n: np.full(n, 1 + 1j, dtype=np.complex64))
    reader = SdrReader(reader_config(swap_iq=True), opener=lambda c: device)
    reader.set_frequency(118_700_000)
    assert wait_until(lambda: reader._q.qsize() > 0)
    block, _ = next(reader.blocks())
    reader.close()
    assert block[0] == 1 - 1j


def test_read_errors_are_counted_and_not_fatal():
    def source(n):
        raise SdrError("unplugged")

    reader = SdrReader(reader_config(), opener=lambda c: FakeDevice(source))
    reader.set_frequency(118_700_000)
    assert wait_until(lambda: reader.read_errors >= 1)
    assert reader._thread.is_alive()
    reader.close()


# -- the real binding, against the real shared library when there is one ------- #

@pytest.mark.librtlsdr
def test_real_librtlsdr_loads_and_every_symbol_exists():
    if ctypes.util.find_library("rtlsdr") is None:
        pytest.skip("librtlsdr not installed")
    sdr._lib = None
    assert isinstance(sdr.list_devices(), list)


# -- the binding, across a real C ABI ------------------------------------------ #

@pytest.fixture
def fake_lib(tmp_path, monkeypatch):
    if shutil.which("cc") is None or not Path("/usr/include/rtl-sdr.h").exists():
        pytest.skip("needs a C compiler and librtlsdr-dev")
    so = tmp_path / "libfakertl.so"
    source = Path(__file__).with_name("fake_librtlsdr.c")
    subprocess.run(["cc", "-shared", "-fPIC", "-o", str(so), str(source)], check=True)
    log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_RTL_LOG", str(log))
    monkeypatch.setattr(ctypes.util, "find_library", lambda name: str(so))
    monkeypatch.setattr(sdr, "_lib", None)

    def calls():
        return [line.split() for line in log.read_text().splitlines()] if log.exists() else []

    yield calls
    sdr._lib = None


def device_config(**over):
    return {"device_serial": "", "device_index": 0, "gain_mode": "manual", "gain_db": 32.8,
            "ppm": 0, "swap_iq": False, "tuner_profiles": {}, **over}


def test_binding_enumerates_serials(fake_lib):
    found = sdr.list_devices()
    assert [d.serial for d in found] == ["00000001", "radio"]
    assert found[1].product == "RTL2838UHIDIR" and found[1].name == "Second"


def test_binding_opens_by_serial(fake_lib):
    device = RtlDevice(device_config(device_serial="radio"))
    device.close()
    assert ["open", "1"] in fake_lib()


def test_binding_configures_in_the_order_librtlsdr_needs(fake_lib):
    device = RtlDevice(device_config())
    device.set_frequency(118_444_000)
    names = [c[0] for c in fake_lib()]
    device.close()
    assert names.index("open") < names.index("rate") < names.index("reset")
    assert ["rate", str(FS_HZ)] in fake_lib()
    assert ["manual", "1"] in fake_lib()
    assert ["gain", "328"] in fake_lib()                 # tenths of a dB
    assert ["tune", "118444000"] in fake_lib()
    assert ["close", "1"] in fake_lib()


def test_binding_uses_agc_when_asked(fake_lib):
    RtlDevice(device_config(gain_mode="agc")).close()
    assert ["manual", "0"] in fake_lib()
    assert "gain" not in [c[0] for c in fake_lib()]


@pytest.mark.parametrize("tuner,requested,label", [
    (FC0012, 32.8, "19.2 dB"),                           # it stops at 19.2
    (FC0012, -4.0, "-4 dB"),
    (R820T, 32.8, "32.8 dB"),
    (R820T, 30.0, "29.7 dB"),
])
def test_binding_snaps_gain_to_the_ladder_of_the_tuner_it_found(fake_lib, monkeypatch,
                                                                tuner, requested, label):
    monkeypatch.setenv("FAKE_RTL_TUNER", tuner)
    device = RtlDevice(device_config(gain_db=requested))
    assert device.gain_label == label
    device.close()


@pytest.mark.parametrize("tuner,name,gain", [(FC0012, "FC0012", "192"), (R820T, "R820T", "328")])
def test_shipped_config_serves_either_tuner_with_nothing_selected(fake_lib, monkeypatch, cfg,
                                                                  tuner, name, gain):
    monkeypatch.setenv("FAKE_RTL_TUNER", tuner)
    device = RtlDevice(cfg.sdr)
    assert device.tuner == name and f"{name} tuner" in device.name
    device.close()
    assert ["gain", gain] in fake_lib()


def test_fc0012_has_no_agc_so_it_gets_manual_gain_and_says_so(fake_lib, monkeypatch, caplog):
    """librtlsdr's fc0012_set_gain_mode is an empty stub: "AGC" would stay at -4 dB."""
    monkeypatch.setenv("FAKE_RTL_TUNER", FC0012)
    with caplog.at_level("WARNING", logger="rbsdrradio.sdr"):
        device = RtlDevice(device_config(gain_mode="agc", gain_db=19.2))
    assert device.gain_label == "19.2 dB"
    device.close()
    assert ["manual", "1"] in fake_lib() and ["gain", "192"] in fake_lib()
    assert "no AGC" in caplog.text


def test_a_dongle_with_no_recognised_tuner_is_refused(fake_lib, monkeypatch):
    """librtlsdr drops to direct sampling, which looks like a working radio that is silent."""
    monkeypatch.setenv("FAKE_RTL_TUNER", "0")
    with pytest.raises(SdrError, match="no supported tuner"):
        RtlDevice(device_config())
    assert ["close", "1"] in fake_lib()


def test_probe_names_the_tuner_and_gives_the_dongle_back(fake_lib, monkeypatch):
    monkeypatch.setenv("FAKE_RTL_TUNER", FC0012)
    assert sdr.probe_tuner(1) == "FC0012"
    assert ["open", "1"] in fake_lib() and ["close", "1"] in fake_lib()


def test_probe_of_a_held_dongle_is_empty_rather_than_an_error(fake_lib, monkeypatch):
    monkeypatch.setenv("FAKE_RTL_BUSY", "1")
    assert sdr.probe_tuner(0) == ""


def test_devices_lists_the_tuner_of_each_dongle(fake_lib, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_RTL_TUNER", FC0012)
    assert main(["devices"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "tuner" in lines[0]
    assert "FC0012" in lines[1] and "00000001" in lines[1]
    assert "FC0012" in lines[2] and "radio" in lines[2]


def test_devices_still_lists_a_dongle_another_program_holds(fake_lib, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_RTL_BUSY", "1")
    assert main(["devices"]) == 0
    assert "in use" in capsys.readouterr().out


def test_binding_names_the_tuner_and_applies_its_profile(fake_lib, monkeypatch):
    monkeypatch.setenv("FAKE_RTL_TUNER", "2")
    device = RtlDevice(device_config(gain_db=32.8, tuner_profiles={
        "FC0012": {"gain_db": 7.1, "swap_iq": True}}))
    assert device.tuner == "FC0012" and device.swap_iq is True
    device.close()
    assert ["gain", "71"] in fake_lib()


def test_binding_tolerates_ppm_already_set(fake_lib):
    """librtlsdr answers -2 for "no change". That is not a failure."""
    device = RtlDevice(device_config(ppm=9))
    device.close()
    assert ["ppm", "9"] in fake_lib()


def test_binding_reads_through_the_buffer(fake_lib):
    device = RtlDevice(device_config())
    iq = device.read(1024)
    device.close()
    assert iq.size == 1024
    assert iq[0] == pytest.approx(1.0 - 1.0j)            # 255,0 in cu8


def test_binding_reports_a_busy_dongle_with_a_useful_message(fake_lib, monkeypatch):
    monkeypatch.setenv("FAKE_RTL_BUSY", "1")
    with pytest.raises(SdrError, match="already in use"):
        RtlDevice(device_config())


def test_binding_raises_on_read_failure(fake_lib, monkeypatch):
    device = RtlDevice(device_config())
    monkeypatch.setenv("FAKE_RTL_READ_FAIL", "1")
    with pytest.raises(SdrError, match="read failed"):
        device.read(1024)
    device.close()


def test_binding_close_is_idempotent(fake_lib):
    device = RtlDevice(device_config())
    device.close()
    device.close()
    assert sum(c[0] == "close" for c in fake_lib()) == 1
