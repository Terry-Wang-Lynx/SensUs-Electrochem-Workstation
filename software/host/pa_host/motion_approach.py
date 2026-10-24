"""Conservative finite phases for approaching captured experiment endpoints."""

FINAL_SPEED_MM_S = 0.5
INTERIOR_CLEARANCE_MM = 1.0


def endpoint_phases(current, target, speed, bounds, pulse_mm, tolerance_mm, *, startup=False):
    lower, upper = bounds
    if not lower <= target <= upper:
        return [(target, speed)]
    if startup:
        return [(target, min(speed, FINAL_SPEED_MM_S))]
    endpoint = min((lower, upper), key=lambda value: abs(value - target))
    if speed <= FINAL_SPEED_MM_S or abs(endpoint - target) > 1e-12:
        return [(target, speed)]
    clearance = min(INTERIOR_CLEARANCE_MM, (upper - lower) / 4)
    if (abs(current - target) <= clearance + tolerance_mm
            or clearance <= max(2 * pulse_mm, 2 * tolerance_mm)):
        return [(target, FINAL_SPEED_MM_S)]
    stage = target + clearance if endpoint == lower else target - clearance
    if (not lower < stage < upper or not min(current, target) < stage < max(current, target)
            or abs(current - stage) <= pulse_mm):
        return [(target, FINAL_SPEED_MM_S)]
    return [(stage, speed), (target, FINAL_SPEED_MM_S)]
