"""What ships: the install files, and the documents staying true to the code."""

import configparser
import json
import re
import subprocess
from pathlib import Path

import pytest

from rbsdrradio.bridge_client import CONSUMED
from rbsdrradio.config import SHIPPED_PATH
from rbsdrradio.publisher import FIELDS

ROOT = Path(__file__).resolve().parents[1]
SPEC = (ROOT / "docs" / "RB-SDR-RADIO-IF-v1.0.md").read_text()
UNIT = (ROOT / "systemd" / "rb-sdr-radio.service").read_text()
INSTALL = ROOT / "install.sh"


def service():
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read_string(UNIT)
    return parser["Service"]


@pytest.mark.parametrize("key", [*FIELDS, *CONSUMED])
def test_interface_document_covers_every_key(key):
    assert f"`{key}`" in SPEC


def test_interface_document_invents_no_key():
    documented = set(re.findall(r"`(sdrradio[a-z]*)`", SPEC))
    assert documented == set(FIELDS) | set(CONSUMED)


def test_every_variable_is_named_sdrradio():
    assert all(key.startswith("sdrradio") for key in [*FIELDS, *CONSUMED])


def test_install_script_parses():
    subprocess.run(["bash", "-n", str(INSTALL)], check=True)


def test_install_script_copies_everything_that_exists():
    text = INSTALL.read_text()
    for name in re.findall(r'"\$SRC/([\w./-]+)"', text):
        assert (ROOT / name).exists(), name


def test_unit_runs_the_real_module():
    assert service()["ExecStart"].split()[-3:] == [
        "rbsdrradio", "--config", "/etc/rb-sdr-radio/rb-sdr-radio.json"]


def test_unit_may_write_the_default_recording_folder():
    folder = json.loads(SHIPPED_PATH.read_text())["record"]["dir"]
    assert service()["ReadWritePaths"].lstrip("-") == folder


def test_unit_does_not_run_as_root():
    assert service()["User"] != "root"


def test_unit_reaches_usb_and_sound():
    groups = service()["SupplementaryGroups"].split()
    assert "plugdev" in groups and "audio" in groups


def test_unit_holds_no_capabilities_and_writes_only_the_recording_folder():
    s = service()
    assert s["CapabilityBoundingSet"] == ""
    assert "AmbientCapabilities" not in s
    assert s["ProtectSystem"] == "strict"


def test_unit_uses_the_watchdog_the_daemon_feeds():
    assert service()["Type"] == "notify"
    assert int(service()["WatchdogSec"]) >= 5


def test_ci_needs_no_hardware():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "pytest" in ci and "ruff" in ci


def test_requirements_match_the_project():
    requirements = (ROOT / "requirements.txt").read_text()
    for package in ("numpy", "scipy", "websockets", "aiohttp", "jsonschema"):
        assert package in requirements


def test_package_never_pulls_a_python_sdr_binding():
    """The ctypes binding exists so a Pi needs only the apt library."""
    for path in (ROOT / "rbsdrradio").glob("*.py"):
        assert not re.search(r"^\s*(import|from) (rtlsdr|SoapySDR)\b", path.read_text(), re.M)
