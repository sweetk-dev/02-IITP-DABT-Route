# -*- coding: utf-8 -*-
import pytest

from route_service.engine.profiles import DEFAULT_PROFILE, get_profile, list_profiles


def test_default_profile_is_manual_wheelchair():
    assert get_profile(None).id == DEFAULT_PROFILE == "wheelchair_manual"


def test_manual_wheelchair_avoids_steps_and_overpass():
    p = get_profile("wheelchair_manual")
    assert "steps" in p.avoid
    assert "overpass" in p.avoid
    assert p.max_slope_deg == 4.0  # 기존 planning.py 의 임계값 유지


def test_electric_wheelchair_allows_steeper_slope():
    assert get_profile("wheelchair_electric").max_slope_deg > get_profile("wheelchair_manual").max_slope_deg


def test_unknown_profile_raises():
    with pytest.raises(KeyError):
        get_profile("no_such_profile")


def test_list_profiles_serializable():
    items = list_profiles()
    assert {"wheelchair_manual", "wheelchair_electric", "crutch", "visual", "walk"} <= {
        i["id"] for i in items
    }
