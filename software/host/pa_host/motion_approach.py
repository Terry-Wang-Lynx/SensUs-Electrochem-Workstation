"""Direct finite phases for approaching captured experiment endpoints."""

FINAL_SPEED_MM_S = 0.5
INTERIOR_CLEARANCE_MM = 0.0


def endpoint_phases(current, target, speed, bounds, pulse_mm, tolerance_mm, *, startup=False):
    lower, upper = bounds
    if not lower <= target <= upper:
        return [(target, speed)]
    return [(target, speed)]
