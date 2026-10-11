"""Load provider profiles (DESIGN §5.3) from config/profiles/*.yaml."""

from __future__ import annotations

from pathlib import Path

import yaml

from raeg_common.models import ProviderProfile

DEFAULT_PROFILE_DIR = Path(__file__).resolve().parents[2] / "config" / "profiles"


def load_profile(path: str | Path) -> ProviderProfile:
    with open(path, encoding="utf-8") as fh:
        return ProviderProfile.model_validate(yaml.safe_load(fh))


def load_all(directory: str | Path = DEFAULT_PROFILE_DIR) -> dict[str, ProviderProfile]:
    profiles = {}
    for path in sorted(Path(directory).glob("*.yaml")):
        profile = load_profile(path)
        if profile.id in profiles:
            raise ValueError(f"duplicate profile id {profile.id} in {path}")
        if path.stem != profile.id:
            raise ValueError(f"profile file {path.name} must be named after its id {profile.id}")
        profiles[profile.id] = profile
    return profiles
