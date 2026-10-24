from pa_host.experiment_progress import Progress


def test_estimate_uses_initial_position_and_current_ack():
    steps = [
        {"id": "one", "op": "move_x", "column": 3, "resolved_params": {"axis": "X", "target_mm": 9, "speed_mm_s": 1}},
        {"id": "two", "op": "dwell", "column": 3, "resolved_params": {"seconds": 10}},
    ]
    progress = Progress(steps, {"X": 8, "Z": 0}, started=100)
    assert progress.snapshot("running", 0, None, 100)["remaining_s"] == 11
    progress.complete(0)
    assert progress.snapshot("running", 1, {"step_id": "old", "elapsed_s": 9, "started_unix_s": 100}, 109)["percent"] < 10
    assert progress.snapshot("paused", 1, None, 109)["remaining_s"] is None
    progress.complete(1)
    assert progress.snapshot("error", 1, None, 110)["percent"] < 100
    assert progress.snapshot("completed", 1, None, 110)["percent"] == 100


def test_estimate_keeps_wrap_and_duplicate_visit_distances():
    steps = [{"id": str(index), "op": "move_x", "column": column,
              "resolved_params": {"axis": "X", "target_mm": target, "speed_mm_s": 1}}
             for index, (column, target) in enumerate([(1, 0), (3, 18), (1, 0), (1, 0)])]
    progress = Progress(steps, {"X": 9, "Z": 0}, started=100)
    assert progress.snapshot("running", 0, None, 100)["remaining_s"] == 45
