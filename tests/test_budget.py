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

def test_named_grants_are_shares_of_usable_not_fixed_seconds():
    # a 120 s and a 900 s deadline must get the SAME fraction of their own usable time
    from solve import DEFAULT_GRANTS
    short, long_ = Budget(120.0, margin_s=15.0, phases=PH), Budget(900.0, margin_s=15.0, phases=PH)
    for name in DEFAULT_GRANTS:
        assert short.frac(name) == DEFAULT_GRANTS[name] * 105.0
        assert long_.frac(name) == DEFAULT_GRANTS[name] * 885.0
    c, b = mk()
    assert b.grant("batch", "batch_reserve") == 0.21 * 285.0     # nothing is spent yet
    c.t += 285 - 25                                              # 25 s left, 20 of them reserved
    assert b.grant("batch", "batch_reserve") == 25.0 - 0.07 * 285.0
    assert b.grant("batch") == 25.0                              # no reserve named, no reserve held

def test_config_grants_override_the_defaults():
    b = Budget(300.0, margin_s=15.0, phases=PH, grants={"batch": 0.5})
    assert b.frac("batch") == 0.5 * 285.0 and b.frac("tiny") == 0.07 * 285.0

def test_can_afford():
    c, b = mk()
    c.t = 100 + 285 - 61
    assert b.can_afford(60) and not b.can_afford(62)
