from osint.core.metrics import Metrics


def test_counters_accumulate_per_label_set():
    m = Metrics()
    m.inc("runs_total", source="a")
    m.inc("runs_total", 2, source="a")
    m.inc("runs_total", source="b")
    assert m.get("runs_total", source="a") == 3
    assert m.get("runs_total", source="b") == 1
    assert m.get("runs_total", source="zzz") == 0


def test_gauges_overwrite():
    m = Metrics()
    m.set("queue_depth", 5)
    m.set("queue_depth", 2)
    assert m.get("queue_depth") == 2


def test_prometheus_rendering_sorted_labels():
    m = Metrics()
    m.inc("runs_total", source="a", result="ok")
    m.set("queue_depth", 4)
    text = m.render_prometheus()
    assert 'runs_total{result="ok",source="a"} 1.0' in text
    assert "queue_depth 4.0" in text
