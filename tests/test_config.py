"""Configuration: the shipped file, and refusing a bad one."""

import json

import pytest

from rbsdrradio.config import SHIPPED_PATH, ConfigError, load


def write(tmp_path, change):
    data = json.loads(SHIPPED_PATH.read_text())
    change(data)
    path = tmp_path / "c.json"
    path.write_text(json.dumps(data))
    return path


def test_shipped_config_is_valid():
    assert load(SHIPPED_PATH).sdr["block_samples"] > 0


def test_default_output_folder_is_the_one_rb01_serves():
    assert load(SHIPPED_PATH).record["dir"] == "/opt/stratux/www/playback"


def test_sdr_selectable_by_index_and_by_serial(tmp_path):
    def pick(d):
        d["sdr"]["device_serial"] = "radio"
        d["sdr"]["device_index"] = 2

    c = load(write(tmp_path, pick))
    assert c.sdr["device_serial"] == "radio" and c.sdr["device_index"] == 2


@pytest.mark.parametrize("change,where", [
    (lambda d: d["sdr"].update(gain_mode="loud"), "gain_mode"),
    (lambda d: d["sdr"].update(typo_key=1), "typo_key"),
    (lambda d: d["sdr"]["tuner_profiles"].update(unknown={}), "unknown"),  # refused at open
    (lambda d: d["radio"].update(volume=150), "volume"),
    (lambda d: d.pop("audio"), "audio"),
    (lambda d: d["record"].update(prefix="a_b"), "prefix"),
    (lambda d: d["bridge"].update(ws_url="http://x"), "ws_url"),
])
def test_invalid_config_is_refused_naming_the_problem(tmp_path, change, where):
    with pytest.raises(ConfigError, match=where):
        load(write(tmp_path, change))


def test_fc0012_steps_below_zero_are_accepted(tmp_path):
    """-9.9 and -4.0 are real steps, the way out when a nearby transmitter clips the ADC."""
    c = load(write(tmp_path, lambda d: d["sdr"]["tuner_profiles"]["FC0012"].update(gain_db=-4.0)))
    assert c.sdr["tuner_profiles"]["FC0012"]["gain_db"] == -4.0


def test_prefix_may_not_contain_an_underscore(tmp_path):
    """radio.js counts `_` fields: an extra one loses the frequency."""
    with pytest.raises(ConfigError):
        load(write(tmp_path, lambda d: d["record"].update(prefix="my_radio")))


def test_one_segment_must_fit_the_quota(tmp_path):
    def change(d):
        d["record"]["max_total_mb"] = 1
        d["record"]["max_segment_s"] = 300

    with pytest.raises(ConfigError, match="max_total_mb"):
        load(write(tmp_path, change))


def test_reconnect_window_must_make_sense(tmp_path):
    with pytest.raises(ConfigError, match="ws_reconnect_max_s"):
        load(write(tmp_path, lambda d: d["bridge"].update(ws_reconnect_max_s=0.1)))


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load(tmp_path / "absent.json")


def test_broken_json(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("{")
    with pytest.raises(ConfigError, match="invalid JSON"):
        load(path)


def test_overrides_do_not_touch_the_original(tmp_path):
    base = load(SHIPPED_PATH)
    changed = base.with_overrides(radio={"volume": 1})
    assert changed.radio["volume"] == 1 and base.radio["volume"] == 50


def test_an_older_config_gets_the_dual_section_of_this_version(tmp_path):
    """install.sh keeps /boot/firmware/rb/rb-sdr-radio.json across an upgrade, and an
    invalid config stops the daemon: the upgrade must not silence the radio."""
    path = write(tmp_path, lambda d: d.pop("dual"))
    shipped = load(SHIPPED_PATH).dual
    assert load(path).dual == shipped


def test_an_older_config_gets_the_demod_section_of_this_version(tmp_path):
    path = write(tmp_path, lambda d: (d.pop("demod"), d.pop("dual")))
    loaded = load(path)
    assert loaded.demod == {"detector": "envelope", "noise_reduction_db": 0}
    assert loaded.dual == load(SHIPPED_PATH).dual


def test_an_older_dual_section_gets_the_keys_added_since(tmp_path):
    path = write(tmp_path, lambda d: (d["dual"].pop("uninterrupted_s"),
                                      d["dual"].update(hold_s=3.0)))
    loaded = load(path)
    assert loaded.dual["uninterrupted_s"] == load(SHIPPED_PATH).dual["uninterrupted_s"]
    assert loaded.dual["hold_s"] == 3.0


def test_an_empty_audio_device_is_valid_and_shipped():
    """A Raspberry Pi 5 has no sound card out of the box."""
    assert load(SHIPPED_PATH).audio["device"] == ""


def test_a_demod_section_is_read_not_replaced(tmp_path):
    path = write(tmp_path, lambda d: d["demod"].update(detector="synchronous",
                                                       noise_reduction_db=12))
    assert load(path).demod == {"detector": "synchronous", "noise_reduction_db": 12}


@pytest.mark.parametrize("change", [{"detector": "coherent"}, {"noise_reduction_db": -1},
                                    {"noise_reduction_db": 21}, {"noise_reduction_db": "on"},
                                    {"surprise": 1}])
def test_a_bad_demod_section_is_refused(tmp_path, change):
    path = write(tmp_path, lambda d: d["demod"].update(change))
    with pytest.raises(ConfigError, match="demod"):
        load(path)


def test_a_dual_section_is_read_not_replaced(tmp_path):
    path = write(tmp_path, lambda d: d["dual"].update(lookback_s=2.5))
    assert load(path).dual["lookback_s"] == 2.5


@pytest.mark.parametrize("change", [{"fill_samples": 16384}, {"dwell_samples": 8192},
                                    {"hop_fail_limit": 0}, {"hold_s": 0},
                                    {"surprise": 1}])
def test_a_bad_dual_section_is_refused(tmp_path, change):
    path = write(tmp_path, lambda d: d["dual"].update(change))
    with pytest.raises(ConfigError, match="dual"):
        load(path)
