"""多分辨率窗口尺寸策略测试。"""

from windows_native.ui.main_window import adaptive_window_geometry


def test_common_resolutions_never_exceed_work_area():
    for width, height in [
        (800, 600),
        (1024, 768),
        (1280, 720),
        (1366, 768),
        (1920, 1040),
        (2560, 1400),
        (3840, 2080),
    ]:
        geometry = adaptive_window_geometry(width, height)
        assert 0 < geometry.width <= width
        assert 0 < geometry.height <= height
        assert geometry.x >= 0
        assert geometry.y >= 0


def test_multi_monitor_origin_is_preserved():
    geometry = adaptive_window_geometry(1920, 1040, -1920, 0)
    assert -1920 <= geometry.x < 0
    assert geometry.y >= 0
