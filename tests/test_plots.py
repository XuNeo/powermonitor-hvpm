import numpy as np
import pytest
from PySide6 import QtCore

from powermonitor.models import Quality
from powermonitor.ui.plots import CurrentViewBox, PowerPlotWidget, make_demo_records
from powermonitor.ui.theme import DARK


class FakeWheelEvent:
    def __init__(self, position: QtCore.QPointF, *, shift: bool = False) -> None:
        self._position = position
        self._modifiers = (
            QtCore.Qt.KeyboardModifier.ShiftModifier
            if shift
            else QtCore.Qt.KeyboardModifier.NoModifier
        )
        self.accepted = False

    def modifiers(self):
        return self._modifiers

    def delta(self) -> int:
        return 120

    def pos(self) -> QtCore.QPointF:
        return self._position

    def accept(self) -> None:
        self.accepted = True

    def ignore(self) -> None:
        self.accepted = False


class FakeDragEvent:
    def __init__(
        self,
        start: QtCore.QPointF,
        end: QtCore.QPointF,
        *,
        finish: bool = True,
    ) -> None:
        self._start = start
        self._end = end
        self._finish = finish

    def accept(self) -> None:
        pass

    def pos(self) -> QtCore.QPointF:
        return self._end

    def lastPos(self) -> QtCore.QPointF:
        return self._start

    def buttonDownPos(self, _button) -> QtCore.QPointF:
        return self._start

    def button(self):
        return QtCore.Qt.MouseButton.LeftButton

    def isFinish(self) -> bool:
        return self._finish


class FakeClickEvent:
    def __init__(self, button=QtCore.Qt.MouseButton.RightButton) -> None:
        self._button = button
        self.accepted = False

    def button(self):
        return self._button

    def accept(self) -> None:
        self.accepted = True


def _shown_widget(qtbot, count: int = 100_000) -> PowerPlotWidget:
    widget = PowerPlotWidget()
    qtbot.addWidget(widget)
    widget.resize(900, 500)
    widget.show()
    qtbot.waitExposed(widget)
    widget.set_records(make_demo_records(count))
    return widget


def test_plot_widget_has_one_current_plot_and_two_curves(qtbot):
    widget = _shown_widget(qtbot, 5000)
    assert len(widget.plots) == 1
    assert set(widget.curves) == {"current", "current_envelope"}
    for curve in widget.curves.values():
        assert curve.opts["downsample"] == 1
        assert not curve.opts["autoDownsample"]
        assert not curve.opts["clipToView"]
    assert widget.curves["current_envelope"].zValue() < widget.curves["current"].zValue()
    assert widget.curves["current_envelope"].opts["connect"] == "pairs"
    assert len(widget.cursor_lines) == 1
    assert len(widget.regions) == 1
    assert isinstance(widget.plots[0].getViewBox(), CurrentViewBox)
    widget.set_theme(DARK)
    assert widget.theme == DARK
    assert widget.curves["current_envelope"].opts["pen"].color().alpha() == 80


def test_wheel_without_shift_zooms_x_only_and_keeps_follow(qtbot):
    widget = _shown_widget(qtbot)
    view_box = widget.plots[0].getViewBox()
    view_box.setRange(xRange=(2.0, 8.0), yRange=(-1.0, 1.0), padding=0)
    follow_events = []
    widget.followChanged.connect(follow_events.append)
    before = view_box.viewRange()
    event = FakeWheelEvent(view_box.boundingRect().center())
    view_box.wheelEvent(event)
    after = view_box.viewRange()
    assert event.accepted
    assert after[0] != pytest.approx(before[0])
    assert after[1] == pytest.approx(before[1])
    assert widget._follow
    assert follow_events == []


def test_shift_wheel_zooms_y_only_without_disabling_follow(qtbot):
    widget = _shown_widget(qtbot)
    view_box = widget.plots[0].getViewBox()
    view_box.setRange(xRange=(2.0, 8.0), yRange=(-1.0, 1.0), padding=0)
    selection = widget.selected_range()
    follow_events = []
    widget.followChanged.connect(follow_events.append)
    before = view_box.viewRange()
    event = FakeWheelEvent(view_box.boundingRect().center(), shift=True)
    view_box.wheelEvent(event)
    after = view_box.viewRange()
    assert event.accepted
    assert after[0] == pytest.approx(before[0])
    assert after[1] != pytest.approx(before[1])
    assert widget._follow
    assert widget.selected_range() == selection
    assert follow_events == []


def test_rect_mode_zooms_x_only_and_selects_x_once(qtbot):
    widget = _shown_widget(qtbot)
    view_box = widget.plots[0].getViewBox()
    view_box.setRange(xRange=(0.0, 20.0), yRange=(-1.0, 1.0), padding=0)
    before = view_box.viewRange()
    events = []
    follow_events = []
    widget.selectionChanged.connect(lambda *values: events.append(values))
    widget.followChanged.connect(follow_events.append)
    rect = view_box.boundingRect()
    drag = FakeDragEvent(
        QtCore.QPointF(rect.left() + rect.width() * 0.25, rect.top() + rect.height() * 0.25),
        QtCore.QPointF(rect.left() + rect.width() * 0.75, rect.top() + rect.height() * 0.75),
    )
    view_box.mouseDragEvent(drag)
    after = view_box.viewRange()
    assert view_box.state["mouseMode"] == view_box.RectMode
    assert after[0][1] - after[0][0] < before[0][1] - before[0][0]
    assert after[1] == pytest.approx(before[1])
    assert widget.selected_range() == pytest.approx(after[0])
    assert widget._follow
    assert follow_events == []
    assert len(events) == 1


def test_live_records_after_rect_keep_width_y_and_follow_latest(qtbot):
    widget = _shown_widget(qtbot)
    view_box = widget.plots[0].getViewBox()
    view_box.setRange(xRange=(0.0, 20.0), yRange=(-1.0, 1.0), padding=0)
    rect = view_box.boundingRect()
    view_box.mouseDragEvent(
        FakeDragEvent(
            QtCore.QPointF(rect.left() + rect.width() * 0.25, rect.top() + rect.height() * 0.25),
            QtCore.QPointF(rect.left() + rect.width() * 0.75, rect.top() + rect.height() * 0.75),
        )
    )
    before_x, before_y = view_box.viewRange()
    width = before_x[1] - before_x[0]
    widget.set_records(
        make_demo_records(110_000),
        live=True,
        preserve_view=True,
        preserve_history=True,
    )
    after_x, after_y = view_box.viewRange()
    assert after_x == pytest.approx((22.0 - width, 22.0))
    assert after_y == pytest.approx(before_y)
    assert widget.selected_range() == pytest.approx(after_x)


def test_right_click_steps_back_wheel_and_rect_history_offline(qtbot):
    widget = _shown_widget(qtbot)
    view_box = widget.plots[0].getViewBox()
    view_box.setRange(xRange=(2.0, 8.0), yRange=(-1.0, 1.0), padding=0)
    initial_x, initial_y = [tuple(values) for values in view_box.viewRange()]

    view_box.wheelEvent(FakeWheelEvent(view_box.boundingRect().center()))
    x_wheel_x, x_wheel_y = [tuple(values) for values in view_box.viewRange()]
    view_box.wheelEvent(FakeWheelEvent(view_box.boundingRect().center(), shift=True))
    y_wheel_x, y_wheel_y = [tuple(values) for values in view_box.viewRange()]

    rect = view_box.boundingRect()
    view_box.mouseDragEvent(
        FakeDragEvent(
            QtCore.QPointF(rect.left() + rect.width() * 0.25, rect.top() + rect.height() * 0.25),
            QtCore.QPointF(rect.left() + rect.width() * 0.75, rect.top() + rect.height() * 0.75),
        )
    )
    events = []
    widget.selectionChanged.connect(lambda *values: events.append(values))

    click = FakeClickEvent()
    view_box.mouseClickEvent(click)
    assert click.accepted
    assert view_box.viewRange()[0] == pytest.approx(y_wheel_x)
    assert view_box.viewRange()[1] == pytest.approx(y_wheel_y)
    assert len(events) == 1

    view_box.mouseClickEvent(FakeClickEvent())
    assert view_box.viewRange()[0] == pytest.approx(x_wheel_x)
    assert view_box.viewRange()[1] == pytest.approx(x_wheel_y)
    assert len(events) == 1  # Undoing Y-only zoom does not change selection.

    view_box.mouseClickEvent(FakeClickEvent())
    assert view_box.viewRange()[0] == pytest.approx(initial_x)
    assert view_box.viewRange()[1] == pytest.approx(initial_y)
    assert len(events) == 2

    empty_click = FakeClickEvent()
    before = view_box.viewRange()
    view_box.mouseClickEvent(empty_click)
    assert empty_click.accepted
    assert view_box.viewRange()[0] == pytest.approx(before[0])
    assert view_box.viewRange()[1] == pytest.approx(before[1])
    assert len(events) == 2


def test_right_click_in_live_follow_reanchors_saved_width_and_y(qtbot):
    widget = _shown_widget(qtbot)
    widget.set_records(make_demo_records(100_000), live=True)
    view_box = widget.plots[0].getViewBox()
    view_box.setRange(xRange=(10.0, 20.0), yRange=(-1.0, 1.0), padding=0)

    view_box.wheelEvent(FakeWheelEvent(view_box.boundingRect().center()))
    pre_y_x, pre_y_y = [tuple(values) for values in view_box.viewRange()]
    view_box.wheelEvent(FakeWheelEvent(view_box.boundingRect().center(), shift=True))
    pre_rect_x, pre_rect_y = [tuple(values) for values in view_box.viewRange()]
    rect = view_box.boundingRect()
    view_box.mouseDragEvent(
        FakeDragEvent(
            QtCore.QPointF(rect.left() + rect.width() * 0.25, rect.top() + rect.height() * 0.25),
            QtCore.QPointF(rect.left() + rect.width() * 0.75, rect.top() + rect.height() * 0.75),
        )
    )
    widget.set_records(
        make_demo_records(110_000),
        live=True,
        preserve_view=True,
        preserve_history=True,
    )

    events = []
    widget.selectionChanged.connect(lambda *values: events.append(values))
    view_box.mouseClickEvent(FakeClickEvent())
    restored_x, restored_y = view_box.viewRange()
    pre_rect_width = pre_rect_x[1] - pre_rect_x[0]
    assert restored_x == pytest.approx((22.0 - pre_rect_width, 22.0))
    assert restored_y == pytest.approx(pre_rect_y)
    assert widget.selected_range() == pytest.approx(restored_x)
    assert widget._follow
    assert len(events) == 1

    view_box.mouseClickEvent(FakeClickEvent())
    restored_x, restored_y = view_box.viewRange()
    pre_y_width = pre_y_x[1] - pre_y_x[0]
    assert restored_x == pytest.approx((22.0 - pre_y_width, 22.0))
    assert restored_y == pytest.approx(pre_y_y)


def test_right_click_with_live_follow_disabled_restores_absolute_range(qtbot):
    widget = _shown_widget(qtbot)
    widget.set_records(make_demo_records(100_000), live=True)
    widget.set_follow_enabled(False)
    view_box = widget.plots[0].getViewBox()
    view_box.setRange(xRange=(2.0, 8.0), yRange=(-1.0, 1.0), padding=0)
    before_x, before_y = [tuple(values) for values in view_box.viewRange()]
    view_box.wheelEvent(FakeWheelEvent(view_box.boundingRect().center()))
    widget.set_records(
        make_demo_records(110_000),
        live=True,
        preserve_view=True,
        preserve_history=True,
    )

    events = []
    widget.selectionChanged.connect(lambda *values: events.append(values))
    view_box.mouseClickEvent(FakeClickEvent())
    assert view_box.viewRange()[0] == pytest.approx(before_x)
    assert view_box.viewRange()[1] == pytest.approx(before_y)
    assert widget.selected_range() == pytest.approx(before_x)
    assert len(events) == 1


def test_live_full_records_cover_full_200_second_bounds(qtbot):
    widget = PowerPlotWidget()
    qtbot.addWidget(widget)
    widget.resize(900, 500)
    records = make_demo_records(1_000_000)
    widget.set_records(records, live=True)
    view_box = widget.plots[0].getViewBox()
    x_range, _y_range = view_box.viewRange()
    assert widget.raw_records() is records
    assert widget._bounds == pytest.approx((0.0, 200.0))
    assert x_range == pytest.approx((190.0, 200.0))


def test_stop_preserve_follow_anchors_final_end_and_keeps_y(qtbot):
    widget = PowerPlotWidget()
    qtbot.addWidget(widget)
    widget.set_records(make_demo_records(100_000), live=True)
    view_box = widget.plots[0].getViewBox()
    view_box.setRange(xRange=(12.0, 20.0), yRange=(-0.5, 0.5), padding=0)
    view_box.sigRangeChangedManually.emit([True, False])

    widget.set_records(
        make_demo_records(105_000),
        live=False,
        preserve_view=True,
        preserve_history=True,
    )

    x_range, y_range = view_box.viewRange()
    assert not widget._live
    assert x_range == pytest.approx((13.0, 21.0))
    assert y_range == pytest.approx((-0.5, 0.5))
    assert widget.selected_range() == pytest.approx(x_range)


def test_stop_preserve_follow_disabled_keeps_absolute_view_selection_and_y(qtbot):
    widget = PowerPlotWidget()
    qtbot.addWidget(widget)
    widget.set_records(make_demo_records(100_000), live=True)
    widget.set_follow_enabled(False)
    view_box = widget.plots[0].getViewBox()
    view_box.setRange(xRange=(2.0, 8.0), yRange=(-0.4, 0.4), padding=0)
    widget.set_selection(3.0, 7.0)

    widget.set_records(
        make_demo_records(110_000),
        live=False,
        preserve_view=True,
        preserve_history=True,
    )

    x_range, y_range = view_box.viewRange()
    assert not widget._live
    assert x_range == pytest.approx((2.0, 8.0))
    assert y_range == pytest.approx((-0.4, 0.4))
    assert widget.selected_range() == pytest.approx((3.0, 7.0))


def test_right_click_after_stop_restores_mapped_follow_history(qtbot):
    widget = PowerPlotWidget()
    qtbot.addWidget(widget)
    widget.resize(900, 500)
    widget.show()
    qtbot.waitExposed(widget)
    widget.set_records(make_demo_records(100_000), live=True)
    view_box = widget.plots[0].getViewBox()
    view_box.setRange(xRange=(10.0, 20.0), yRange=(-1.0, 1.0), padding=0)

    initial_x, initial_y = [tuple(values) for values in view_box.viewRange()]
    view_box.wheelEvent(FakeWheelEvent(view_box.boundingRect().center()))
    x_wheel_x, x_wheel_y = [tuple(values) for values in view_box.viewRange()]
    view_box.wheelEvent(FakeWheelEvent(view_box.boundingRect().center(), shift=True))
    y_wheel_x, y_wheel_y = [tuple(values) for values in view_box.viewRange()]
    rect = view_box.boundingRect()
    view_box.mouseDragEvent(
        FakeDragEvent(
            QtCore.QPointF(rect.left() + rect.width() * 0.25, rect.top() + rect.height() * 0.25),
            QtCore.QPointF(rect.left() + rect.width() * 0.75, rect.top() + rect.height() * 0.75),
        )
    )

    widget.set_records(
        make_demo_records(110_000),
        live=False,
        preserve_view=True,
        preserve_history=True,
    )

    view_box.mouseClickEvent(FakeClickEvent())
    restored_x, restored_y = view_box.viewRange()
    y_wheel_width = y_wheel_x[1] - y_wheel_x[0]
    assert restored_x == pytest.approx((22.0 - y_wheel_width, 22.0))
    assert restored_y == pytest.approx(y_wheel_y)

    view_box.mouseClickEvent(FakeClickEvent())
    restored_x, restored_y = view_box.viewRange()
    x_wheel_width = x_wheel_x[1] - x_wheel_x[0]
    assert restored_x == pytest.approx((22.0 - x_wheel_width, 22.0))
    assert restored_y == pytest.approx(x_wheel_y)

    view_box.mouseClickEvent(FakeClickEvent())
    restored_x, restored_y = view_box.viewRange()
    initial_width = initial_x[1] - initial_x[0]
    assert restored_x == pytest.approx((22.0 - initial_width, 22.0))
    assert restored_y == pytest.approx(initial_y)


def test_empty_records_clear_history_so_right_click_is_noop(qtbot):
    widget = _shown_widget(qtbot)
    view_box = widget.plots[0].getViewBox()
    view_box.setRange(xRange=(2.0, 8.0), yRange=(-1.0, 1.0), padding=0)
    view_box.wheelEvent(FakeWheelEvent(view_box.boundingRect().center()))

    widget.set_records(None)

    before_x, before_y = [tuple(values) for values in view_box.viewRange()]
    click = FakeClickEvent()
    view_box.mouseClickEvent(click)
    assert click.accepted
    assert view_box.viewRange()[0] == pytest.approx(before_x)
    assert view_box.viewRange()[1] == pytest.approx(before_y)


def test_manual_new_open_clears_history_and_shows_full_range(qtbot):
    widget = _shown_widget(qtbot)
    view_box = widget.plots[0].getViewBox()
    view_box.setRange(xRange=(2.0, 8.0), yRange=(-1.0, 1.0), padding=0)
    view_box.wheelEvent(FakeWheelEvent(view_box.boundingRect().center()))

    records = make_demo_records(1000)
    widget.set_records(records)

    before_x, before_y = [tuple(values) for values in view_box.viewRange()]
    view_box.mouseClickEvent(FakeClickEvent())
    assert before_x == pytest.approx((0.0, 0.2))
    assert view_box.viewRange()[0] == pytest.approx(before_x)
    assert view_box.viewRange()[1] == pytest.approx(before_y)


def test_new_source_reinitializes_y_range(qtbot):
    widget = PowerPlotWidget()
    qtbot.addWidget(widget)
    first = make_demo_records(1000)
    first["current_a"] = 0.05
    widget.set_records(first)
    view_box = widget.plots[0].getViewBox()
    view_box.setYRange(-1.0, 1.0, padding=0)

    second = make_demo_records(1000)
    second["current_a"] = 5.0
    widget.set_records(second)

    y_range = view_box.viewRange()[1]
    assert y_range[0] > 4.0
    assert y_range[1] < 6.0


def test_region_is_display_only_but_numeric_selection_updates_it(qtbot):
    widget = _shown_widget(qtbot, 5000)
    assert not widget.region.movable
    assert widget.region.acceptedMouseButtons() == QtCore.Qt.MouseButton.NoButton
    assert all(
        edge.acceptedMouseButtons() == QtCore.Qt.MouseButton.NoButton
        for edge in widget.region.lines
    )
    widget.set_selection(0.2, 0.4)
    assert widget.selected_range() == pytest.approx((0.2, 0.4))
    assert widget.region.getRegion() == pytest.approx((0.2, 0.4))


def test_manual_x_range_keeps_follow_and_updates_selection_once(qtbot):
    widget = _shown_widget(qtbot)
    view_box = widget.plots[0].getViewBox()
    follow_events = []
    selection_events = []
    widget.followChanged.connect(follow_events.append)
    widget.selectionChanged.connect(lambda *values: selection_events.append(values))
    view_box.setXRange(1.0, 2.0, padding=0)
    view_box.sigRangeChangedManually.emit([True, False])
    assert widget._follow
    assert follow_events == []
    assert widget.selected_range() == pytest.approx((1.0, 2.0))
    assert selection_events == [pytest.approx((1.0, 2.0))]


def test_live_records_reanchor_follow_width_and_keep_y_range(qtbot):
    widget = _shown_widget(qtbot)
    view_box = widget.plots[0].getViewBox()
    view_box.setRange(xRange=(1.0, 2.0), yRange=(-0.5, 0.5), padding=0)
    view_box.sigRangeChangedManually.emit([True, False])
    widget.set_records(
        make_demo_records(110_000),
        live=True,
        preserve_view=True,
        preserve_history=True,
    )
    x_range, y_range = view_box.viewRange()
    assert x_range == pytest.approx((21.0, 22.0))
    assert y_range == pytest.approx((-0.5, 0.5))
    assert widget.selected_range() == pytest.approx(x_range)


def test_follow_false_keeps_view_static_during_live_records(qtbot):
    widget = _shown_widget(qtbot)
    view_box = widget.plots[0].getViewBox()
    view_box.setRange(xRange=(1.0, 2.0), yRange=(-0.5, 0.5), padding=0)
    widget.set_follow_enabled(False)
    frozen = view_box.viewRange()
    widget.set_records(
        make_demo_records(110_000),
        live=True,
        preserve_view=True,
        preserve_history=True,
    )
    assert view_box.viewRange()[0] == pytest.approx(frozen[0])
    assert view_box.viewRange()[1] == pytest.approx(frozen[1])


def test_reenabling_follow_keeps_current_width_and_syncs_selection(qtbot):
    widget = _shown_widget(qtbot)
    view_box = widget.plots[0].getViewBox()
    widget.set_follow_enabled(False)
    view_box.setRange(xRange=(1.0, 2.0), yRange=(-0.5, 0.5), padding=0)
    widget.set_follow_enabled(True)
    x_range, y_range = view_box.viewRange()
    assert x_range == pytest.approx((19.0, 20.0))
    assert y_range == pytest.approx((-0.5, 0.5))
    assert widget.selected_range() == pytest.approx(x_range)


def test_render_signature_skips_same_static_historical_view(qtbot, monkeypatch):
    widget = _shown_widget(qtbot)
    view_box = widget.plots[0].getViewBox()
    widget.set_follow_enabled(False)
    view_box.setXRange(1.0, 2.0, padding=0)
    view_box.sigRangeChangedManually.emit([True, False])
    widget._update_visible_data()
    calls = []
    original = widget.curves["current"].setData

    def counting_set_data(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(widget.curves["current"], "setData", counting_set_data)
    widget._update_visible_data()
    widget.set_records(
        make_demo_records(110_000),
        live=True,
        preserve_view=True,
        preserve_history=True,
    )
    assert calls == []


def test_offline_set_records_populates_curve_immediately(qtbot):
    widget = PowerPlotWidget()
    qtbot.addWidget(widget)
    widget.set_records(make_demo_records(1000))
    x, y = widget.curves["current"].getOriginalDataset()
    assert x is not None and y is not None
    assert len(x) == len(y) > 0


def test_invalid_flag_breaks_waveform_and_does_not_change_raw_data(qtbot):
    widget = PowerPlotWidget()
    qtbot.addWidget(widget)
    records = make_demo_records(20)
    records["flags"][10] = Quality.INVALID
    widget.set_records(records)
    _x, y = widget.curves["current"].getOriginalDataset()
    assert y is not None
    assert np.any(~np.isfinite(y))
    assert np.all(np.isfinite(records["current_a"]))
    assert widget.raw_records() is records


def test_cursor_uses_only_current_flags_and_dropped(qtbot):
    widget = PowerPlotWidget()
    qtbot.addWidget(widget)
    widget.set_records(make_demo_records(1000))
    widget._show_cursor(0.02)
    text = widget.cursor_label.text()
    assert "Current" in text
    assert "flags" in text and "dropped" in text
    assert "Voltage" not in text and "Power" not in text
    widget.setFocus()
    qtbot.keyClick(widget, QtCore.Qt.Key.Key_Right)
    assert widget.cursor_lines[0].value() == pytest.approx(0.0202)
