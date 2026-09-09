"""`.env.example` is what people copy to make their `.env`, and a `.env` wins over
every default in the code. When the template drifts from the shipped defaults, the
bot silently runs the old rules on a machine nobody can inspect - which is exactly
how a stale ALLOW_WITHOUT_RESEARCH=false stops every trade for a week.
"""
from kalshitrader.config import Settings, load_settings

# The template is allowed to differ only where it is naming a local file rather than
# choosing a behaviour.
ALLOWED_DIFFERENCES = {"kalshi_private_key_path"}


def test_env_example_matches_the_shipped_defaults():
    example, defaults = load_settings(".env.example"), Settings()
    drifted = {
        f: (getattr(example, f), getattr(defaults, f))
        for f in defaults.__dataclass_fields__
        if f not in ALLOWED_DIFFERENCES and getattr(example, f) != getattr(defaults, f)
    }
    assert not drifted, (
        "`.env.example` disagrees with the defaults in config.py; update the template "
        f"(field: example value vs default): {drifted}")
