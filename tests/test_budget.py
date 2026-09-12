from solve import Budget
PH = {"generate_until": 0.32, "gate_until": 0.39, "repair_until": 0.81, "settle_until": 0.93}

class Clock:
    def __init__(self): self.t = 100.0
    def __call__(self): return self.t

def mk():
    c = Clock(); return c, Budget(300.0, margin_s=15.0, phases=PH, clock=c)

def test_usable_and_remaining():
    c, b = mk()
    assert b.usable_s == 285.0 and b.remaining() == 285.0
    c.t += 100; assert b.remaining() == 185.0

def test_phase_transitions():
    c, b = mk()
    assert b.phase() == "generate"
    c.t = 100 + 0.33 * 285; assert b.phase() == "gate"
    c.t = 100 + 0.40 * 285; assert b.phase() == "repair"
    c.t = 100 + 0.82 * 285; assert b.phase() == "settle"
    c.t = 100 + 0.94 * 285; assert b.phase() == "emit"
    c.t = 100 + 286; assert b.phase() == "expired" and b.remaining() == 0.0

def test_step_timeout_is_min_of_cap_and_remaining_minus_reserve():
    c, b = mk()
    assert b.step_timeout(90.0) == 90.0
    c.t = 100 + 285 - 50
    assert b.step_timeout(90.0, reserve_s=20.0) == 30.0
    c.t = 100 + 285 - 5
    assert b.step_timeout(90.0, reserve_s=20.0) == 0.0

def test_can_afford():
    c, b = mk()
    c.t = 100 + 285 - 61
    assert b.can_afford(60) and not b.can_afford(62)
