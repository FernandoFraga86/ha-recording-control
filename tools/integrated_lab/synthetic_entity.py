"""Select the unique Home Assistant entity created by this synthetic MQTT probe."""

from __future__ import annotations

from typing import Any


def select_synthetic_sensor_state(states: object, run_id: str) -> str | None:
    if not isinstance(states, list):
        return None

    expected_suffix = "ci_frigate_mqtt_probe_" + run_id
    expected_name = "CI Frigate MQTT Probe " + run_id
    matches: list[dict[str, Any]] = []
    for state in states:
        if not isinstance(state, dict):
            continue
        entity_id = state.get("entity_id")
        attrs = state.get("attributes")
        friendly_name = attrs.get("friendly_name") if isinstance(attrs, dict) else None
        if (
            isinstance(entity_id, str)
            and entity_id.startswith("sensor.")
            and entity_id.endswith(expected_suffix)
        ) or friendly_name == expected_name:
            matches.append(state)

    if len(matches) != 1:
        return None
    value = matches[0].get("state")
    return value if isinstance(value, str) else None
