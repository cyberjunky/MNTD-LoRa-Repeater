from lorarepeater import led


def test_health_state():
    assert led.health_state(True, True, True) == "green"
    assert led.health_state(True, False, True) == "orange"
    assert led.health_state(True, True, False) == "orange"
    assert led.health_state(False, False, False) == "red"


def test_set_state_drives_both_leds(tmp_path, monkeypatch):
    for name in (led.GREEN, led.RED):
        (tmp_path / name).mkdir()
        (tmp_path / name / "brightness").write_text("0")
        (tmp_path / name / "trigger").write_text("mmc0")
    monkeypatch.setattr(led, "LEDS", tmp_path)
    led.set_state("orange")
    assert (tmp_path / led.GREEN / "brightness").read_text() == "1"
    assert (tmp_path / led.RED / "brightness").read_text() == "1"
    assert (tmp_path / led.RED / "trigger").read_text() == "none"
    led.set_state("red")
    assert (tmp_path / led.GREEN / "brightness").read_text() == "0"
    led.set_state("off")
    assert (tmp_path / led.GREEN / "trigger").read_text() == "mmc0"
    assert (tmp_path / led.RED / "trigger").read_text() == "default-on"


def test_set_state_without_leds_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(led, "LEDS", tmp_path / "missing")
    led.set_state("green")
