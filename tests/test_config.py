import tomllib, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent

def test_config_has_openrouter_roles_and_cost_cap():
    cfg = tomllib.loads((ROOT / "config.toml").read_text())
    for role in ("strong", "fast"):
        r = cfg["profiles"]["openrouter"][role]
        assert r["base_url"] == "https://openrouter.ai/api/v1"
        assert r["api_key_env"] == "OPENROUTER_API_KEY"
        assert r["max_concurrent"] >= 1 and r["max_tokens"] > 0
        assert isinstance(r["extra"], dict)   # merged verbatim into the request body
        assert r["model"]
    # "strong" and "fast" are roles, not a promise of two different models: the fast role may be
    # pointed at the strong model when the cheaper one cannot write a usable oracle inside the cap
    # (round 13). Independence of oracle and candidate is then bought by the prompts and the
    # SOLVE-authored examples, not by the model slug.
    assert 0 < cfg["limits"]["max_cost_usd_per_problem"] <= 2.0
    assert cfg["limits"]["safety_margin_s"] > 0
    assert cfg["phases"]["generate_until"] < cfg["phases"]["repair_until"] < cfg["phases"]["settle_until"] < 1

def test_roles_fallback_names_a_role_every_profile_has():
    # A refusal falls back to this role (solve.Run.chat); a typo here would silently mean "no
    # fallback" for the profile that lacks it.
    cfg = tomllib.loads((ROOT / "config.toml").read_text())
    fb = cfg["roles"].get("fallback")
    assert fb is None or all(fb in prof for prof in cfg["profiles"].values())
