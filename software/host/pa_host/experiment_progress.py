"""Pure bounded estimates for an immutable experiment plan."""

from .motion_approach import endpoint_phases


class Progress:
    def __init__(self, steps, initial_position, execution=None, *, started, calibration=None):
        self.steps = steps
        self.started = started
        self.finished = None
        self.completed = 0
        self.active_index = None
        self.active_started = None
        self.time_scale = 1
        self.durations = []
        position = dict(initial_position)
        self.visits = []
        for index, step in enumerate(steps):
            params = step["resolved_params"]
            if step["op"].startswith("move_"):
                axis = params["axis"]
                duration = abs(position[axis] - params["target_mm"]) / params["speed_mm_s"]
                if calibration is not None:
                    bounds = (sorted((calibration["x_first_mm"], calibration["x_last_mm"])) if axis == "X"
                              else [calibration["z_safe_mm"], calibration["z_working_mm"]])
                    lead = calibration["motion"]["axes"][axis]["lead_mm"]
                    profile = calibration["reference"]["profiles"][axis]["config"]
                    tolerance = max(lead / 65536, lead * profile["position_window_deg"] / 360)
                    startup = axis == "Z" and not bounds[0] <= position[axis] <= bounds[1]
                    phases = endpoint_phases(position[axis], params["target_mm"], params["speed_mm_s"], bounds,
                        lead / profile["pulses_per_rev"], tolerance, startup=startup)
                    previous, duration = position[axis], 0
                    for phase_target, phase_speed in phases:
                        duration += abs(previous - phase_target) / phase_speed
                        previous = phase_target
                position[axis] = params["target_mm"]
            else:
                duration = params.get("seconds", 0)
            self.durations.append(duration)
            if step["op"] == "dwell":
                self.visits.append((index, step["column"]))
        self.execution = execution or {"mode": "once", "count": 1, "route": [column for _, column in self.visits]}

    def begin(self, index, now, time_scale=1):
        self.active_index = index
        self.active_started = now
        self.time_scale = time_scale

    def complete(self, index):
        self.completed = max(self.completed, index + 1)

    def snapshot(self, state, index, timing, now):
        if self.finished is not None:
            now = self.finished
        suspended = state in {"paused", "waiting_operator", "waiting_recovery", "recovering", "stopping"}
        fraction = 0.0
        if 0 <= index < len(self.steps) and index >= self.completed:
            step = self.steps[index]
            if (step["op"] == "dwell" and timing and timing.get("step_id") == step["id"]
                    and timing.get("started_unix_s") is not None):
                fraction = min(self.durations[index], max(0, timing.get("elapsed_s", 0)))
            elif (step["op"] != "dwell" and self.active_index == index and self.active_started is not None
                    and not suspended and self.time_scale > 0):
                fraction = min(self.durations[index], max(0, now - self.active_started) / self.time_scale)
        total = sum(self.durations)
        consumed = sum(self.durations[:self.completed]) + fraction
        percent = min(99.99, 100 * consumed / total) if total else min(99.99, 100 * self.completed / max(1, len(self.steps)))
        visit_ends = []
        for offset, (dwell_index, _) in enumerate(self.visits):
            next_dwell = self.visits[offset + 1][0] if offset + 1 < len(self.visits) else len(self.steps)
            boundary = next((step_index for step_index in range(dwell_index + 1, next_dwell)
                if self.steps[step_index]["resolved_params"].get("purpose") == "safe_before_x"), len(self.steps))
            visit_ends.append(boundary - 1)
        completed_visits = sum(visit_end < self.completed for visit_end in visit_ends)
        visit = next((offset for offset, visit_end in enumerate(visit_ends) if visit_end >= index), max(0, len(self.visits) - 1))
        if 0 <= index < len(self.steps) and "visit_index" in self.steps[index]:
            visit = self.steps[index]["visit_index"] - 1
        route = self.execution["route"]
        path_length = len(route)
        remaining = max(0, total - consumed)
        if state == "completed":
            percent, remaining = 100, 0
        if suspended or state in {"error", "stopped"}:
            remaining = None
        return {"percent": percent, "completed_steps": self.completed, "total_steps": len(self.steps),
                "completed_visits": completed_visits, "total_visits": len(self.visits),
                "cycle_index": visit // max(1, path_length) + 1, "total_cycles": self.execution["count"],
                "path_index": visit % max(1, path_length) + 1, "path_length": path_length,
                "elapsed_s": max(0, (self.finished if self.finished is not None else now) - self.started),
                "remaining_s": remaining, "estimated_finish_unix_s": None if remaining is None else now + remaining,
                "suspended": suspended, "route": list(route), "mode": self.execution["mode"]}
