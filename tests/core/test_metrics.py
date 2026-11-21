import sys
import threading

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


def test_concurrent_increments_are_not_lost():
    m = Metrics()
    n_threads, n_incs = 4, 10_000
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # force frequent thread switches to expose races
    try:
        def work():
            for _ in range(n_incs):
                m.inc("hits_total", source="a")
                m.render_prometheus() if _ % 2500 == 0 else None

        threads = [threading.Thread(target=work) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        sys.setswitchinterval(old)
    assert m.get("hits_total", source="a") == n_threads * n_incs


def test_mutations_and_rendering_are_serialised_by_lock():
    m = Metrics()
    with m._lock:
        writer = threading.Thread(target=m.inc, args=("x_total",))
        reader = threading.Thread(target=m.render_prometheus)
        writer.start()
        reader.start()
        writer.join(0.1)
        reader.join(0.1)
        assert writer.is_alive() and reader.is_alive()  # both wait for the lock
    writer.join(5)
    reader.join(5)
    assert m.get("x_total") == 1
