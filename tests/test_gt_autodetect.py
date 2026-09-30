"""Headless tests: OBDX Pro GT port detection (gt.py) matches by USB VID/PID
only and never falls back to another serial port. No serial hardware involved;
ports are stand-ins shaped like pyserial's ListPortInfo."""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openobd import gt  # noqa: E402


def port(device, description, vid=None, pid=None):
    return SimpleNamespace(device=device, description=description,
                           vid=vid, pid=pid)


GT = port("COM3", "USB Serial Device (COM3)", 0x0483, 0x5740)
MXPLUS = port("COM4", "Standard Serial over Bluetooth link (COM4)")
BT_IN = port("COM5", "Standard Serial over Bluetooth link (COM5)")
FTDI = port("COM7", "USB Serial Port (COM7)", 0x0403, 0x6001)
OTHER_STM = port("COM8", "USB Serial Device (COM8)", 0x0483, 0x374B)


@pytest.fixture
def ports(monkeypatch):
    """Point the live enumeration at a chosen port list."""
    def use(*ps):
        monkeypatch.setattr(gt, "list_ports",
                            SimpleNamespace(comports=lambda: list(ps)))
    return use


def test_finds_gt_by_vid_pid_among_others(ports):
    ports(MXPLUS, BT_IN, GT, FTDI)
    assert gt.ObdxGt.autodetect() == "COM3"


def test_no_gt_returns_none_not_another_usb_serial(ports):
    # The old fallback took any port with a USB vid (FTDI here) or the
    # first port at all (the MX+) - both wrong adapters.
    ports(MXPLUS, BT_IN, FTDI, OTHER_STM)
    assert gt.ObdxGt.autodetect() is None


def test_no_ports_at_all(ports):
    ports()
    assert gt.ObdxGt.autodetect() is None
    assert "Serial ports present: none" in gt.describe_no_gt()


def test_two_gts_is_ambiguous(ports):
    second = port("COM9", "USB Serial Device (COM9)", 0x0483, 0x5740)
    ports(GT, second)
    assert gt.ObdxGt.autodetect() is None
    msg = gt.describe_no_gt()
    assert "More than one" in msg and "COM3" in msg and "COM9" in msg


def test_same_vendor_other_product_is_not_a_gt(ports):
    ports(OTHER_STM)
    assert gt.find_gt_ports() == []


def test_error_names_every_port_seen(ports):
    ports(MXPLUS, FTDI)
    msg = gt.describe_no_gt()
    assert "0483:5740" in msg
    assert "COM4 (Standard Serial over Bluetooth link (COM4))" in msg
    assert "COM7" in msg


def test_open_refuses_without_a_gt(ports, monkeypatch):
    ports(MXPLUS, FTDI)
    opened = []
    monkeypatch.setattr(gt, "serial", SimpleNamespace(
        Serial=lambda *a, **k: opened.append(a)))
    with pytest.raises(RuntimeError, match="No OBDX Pro GT"):
        gt.ObdxGt().open()
    assert opened == []  # nothing was opened, so nothing was written


def test_explicit_port_bypasses_detection(ports, monkeypatch):
    ports()  # no GT visible; the caller named a port, so trust it
    opened = []

    class Stop(Exception):
        pass

    def fake_serial(name, *a, **k):
        opened.append(name)
        raise Stop
    monkeypatch.setattr(gt, "serial", SimpleNamespace(Serial=fake_serial))
    with pytest.raises(Stop):
        gt.ObdxGt(port="COM6").open()
    assert opened == ["COM6"]


def test_without_pyserial_nothing_is_found(monkeypatch):
    monkeypatch.setattr(gt, "list_ports", None)
    assert gt.find_gt_ports() == []
    assert gt.ObdxGt.autodetect() is None
