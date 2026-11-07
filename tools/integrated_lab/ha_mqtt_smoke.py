"""Configure Core's real MQTT integration and verify a synthetic MQTT state."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import secrets
import sys
import time

import aiohttp
from frigate_version import parse_frigate_version


BASE = "http://127.0.0.1:8123"
STATUS = Path("/lab-control/ha-result.json")
CURRENT_STEP = "startup"
CURRENT_DIAGNOSTIC: str | None = None


def write_status(phase: str, **fields: object) -> None:
    payload = {"phase": phase, **fields}
    temporary = STATUS.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    temporary.replace(STATUS)


async def request_json(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    payload: object | None = None,
    form: dict[str, str] | None = None,
    token: str | None = None,
) -> tuple[int, object]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with session.request(method, url, json=payload, data=form, headers=headers) as response:
        text = await response.text()
        try:
            result: object = json.loads(text) if text else {}
        except json.JSONDecodeError:
            result = {"body_type": "non_json"}
        return response.status, result


async def main(run_id: str) -> None:
    global CURRENT_STEP, CURRENT_DIAGNOSTIC
    timeout = aiohttp.ClientTimeout(total=12)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        CURRENT_STEP = "wait_for_core_onboarding"
        deadline = time.monotonic() + 240
        onboarding: object = None
        while time.monotonic() < deadline:
            try:
                status, onboarding = await request_json(session, "GET", BASE + "/api/onboarding")
                if status == 200:
                    break
            except (aiohttp.ClientError, asyncio.TimeoutError):
                await asyncio.sleep(2)
        else:
            raise RuntimeError("Core onboarding API did not become ready")
        if not isinstance(onboarding, list) or any(
            item.get("step") == "user" and item.get("done")
            for item in onboarding
            if isinstance(item, dict)
        ):
            raise RuntimeError("Core did not present a fresh disposable onboarding state")

        username = "ci_lab_" + secrets.token_hex(8)
        password = secrets.token_urlsafe(32)
        client_id = "http://localhost:8123/"
        CURRENT_STEP = "create_disposable_onboarding_user"
        status, created = await request_json(
            session,
            "POST",
            BASE + "/api/onboarding/users",
            payload={
                "name": "CI Synthetic Admin",
                "username": username,
                "password": password,
                "client_id": client_id,
                "language": "en",
            },
        )
        if status != 200 or not isinstance(created, dict) or not created.get("auth_code"):
            raise RuntimeError("disposable Core onboarding user creation failed")

        CURRENT_STEP = "exchange_disposable_auth_code"
        status, token_result = await request_json(
            session,
            "POST",
            BASE + "/auth/token",
            form={
                "grant_type": "authorization_code",
                "code": str(created["auth_code"]),
                "client_id": client_id,
            },
        )
        if status != 200 or not isinstance(token_result, dict) or not token_result.get("access_token"):
            raise RuntimeError("disposable Core API token exchange failed")
        token = str(token_result["access_token"])

        CURRENT_STEP = "verify_authenticated_core_api"
        status, api_health = await request_json(session, "GET", BASE + "/api/", token=token)
        if status != 200 or not isinstance(api_health, dict) or api_health.get("message") != "API running.":
            if isinstance(api_health, dict):
                keys = ",".join(sorted(str(key) for key in api_health.keys())[:12])
                message = str(api_health.get("message", ""))[:120]
                CURRENT_DIAGNOSTIC = f"http_status={status}; payload_keys={keys}; message={message!r}"
            else:
                CURRENT_DIAGNOSTIC = f"http_status={status}; payload_type={type(api_health).__name__}"
            raise RuntimeError("authenticated Core REST API probe failed")

        CURRENT_STEP = "open_real_mqtt_config_flow"
        status, flow = await request_json(
            session,
            "POST",
            BASE + "/api/config/config_entries/flow",
            payload={"handler": "mqtt"},
            token=token,
        )
        if status != 200 or not isinstance(flow, dict) or flow.get("type") != "form" or not flow.get("flow_id"):
            raise RuntimeError("Core MQTT config flow did not open its broker form")
        flow_id = str(flow["flow_id"])
        config = {
            "broker": "mosquitto",
            "port": 1883,
            "protocol": "5",
            "username": "",
            "password": "",
            "other_settings": {
                "set_client_cert": False,
                "set_ca_cert": "off",
                "transport": "tcp",
            },
        }
        CURRENT_STEP = "submit_real_mqtt_config_flow"
        status, configured = await request_json(
            session,
            "POST",
            BASE + f"/api/config/config_entries/flow/{flow_id}",
            payload=config,
            token=token,
        )
        if status != 200 or not isinstance(configured, dict) or configured.get("type") != "create_entry":
            errors = configured.get("errors") if isinstance(configured, dict) else None
            result_type = configured.get("type") if isinstance(configured, dict) else "unknown"
            raise RuntimeError(
                f"Core MQTT config flow did not create the broker entry; "
                f"result_type={result_type}; errors={errors}"
            )

        CURRENT_STEP = "wait_for_loaded_mqtt_config_entry"
        entry_deadline = time.monotonic() + 45
        mqtt_entry_loaded = False
        while time.monotonic() < entry_deadline:
            status, entries = await request_json(
                session,
                "GET",
                BASE + "/api/config/config_entries/entry?domain=mqtt",
                token=token,
            )
            if status == 200 and isinstance(entries, list):
                mqtt_entry_loaded = any(
                    isinstance(entry, dict) and entry.get("state") == "loaded"
                    for entry in entries
                )
                if mqtt_entry_loaded:
                    break
            await asyncio.sleep(1)
        if not mqtt_entry_loaded:
            raise RuntimeError("Core MQTT config entry was not observed in loaded state")

        entity_id = "sensor.ci_frigate_mqtt_probe_" + run_id
        write_status(
            "mqtt_ready",
            mqtt_entry_loaded=True,
            entity_id=entity_id,
            run_id=run_id,
        )

        CURRENT_STEP = "verify_core_to_frigate_api"
        frigate_version: str | None = None
        api_deadline = time.monotonic() + 180
        while time.monotonic() < api_deadline:
            try:
                async with session.get("http://frigate:5000/api/version") as response:
                    status = response.status
                    body = await response.text()
                if status == 200:
                    try:
                        frigate_version = parse_frigate_version(body)
                    except ValueError as error:
                        raise RuntimeError("Core reached an unexpected Frigate API version") from error
                    break
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass
            await asyncio.sleep(2)
        if frigate_version is None:
            raise RuntimeError("Core could not reach the Frigate API over the isolated network")

        CURRENT_STEP = "observe_synthetic_mqtt_entity_state"
        event_deadline = time.monotonic() + 90
        observed: str | None = None
        while time.monotonic() < event_deadline:
            status, states = await request_json(
                session,
                "GET",
                BASE + "/api/states",
                token=token,
            )
            if status == 200 and isinstance(states, list):
                matches: list[tuple[str, str | None]] = []
                for state in states:
                    if not isinstance(state, dict):
                        continue
                    attrs = state.get("attributes")
                    current_entity_id = state.get("entity_id")
                    friendly_name = attrs.get("friendly_name") if isinstance(attrs, dict) else None
                    if (
                        isinstance(current_entity_id, str)
                        and "ci_frigate_mqtt_probe" in current_entity_id
                    ) or (
                        isinstance(friendly_name, str)
                        and friendly_name.startswith("CI Frigate MQTT Probe ")
                    ):
                        matches.append((str(current_entity_id), str(state.get("state"))))
                    if state.get("entity_id") == entity_id or (
                        isinstance(attrs, dict)
                        and attrs.get("friendly_name") == "CI Frigate MQTT Probe " + run_id
                    ):
                        observed = str(state.get("state"))
                        break
                CURRENT_DIAGNOSTIC = (
                    f"http_status={status}; states_count={len(states)}; "
                    f"target_entity={entity_id}; matching_synthetic_entities={matches[:5]!r}"
                )
            else:
                CURRENT_DIAGNOSTIC = (
                    f"http_status={status}; states_payload_type={type(states).__name__}"
                )
            if observed == "synthetic_event_received":
                write_status(
                    "passed",
                    mqtt_entry_loaded=True,
                    mqtt_synthetic_entity_state=observed,
                    frigate_api_version=frigate_version,
                    run_id=run_id,
                )
                return
            await asyncio.sleep(1)
        raise RuntimeError(
            f"Core did not observe the synthetic MQTT discovery/state; entity_state={observed!r}"
        )


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise RuntimeError("run id argument missing")
        asyncio.run(main(sys.argv[1]))
    except BaseException as error:  # noqa: BLE001 - write fail-closed status to runner temp.
        try:
            write_status(
                "failed",
                step=CURRENT_STEP,
                error_type=type(error).__name__,
                diagnostic=CURRENT_DIAGNOSTIC,
            )
        except OSError:
            pass
        print(f"HA synthetic MQTT lab failed: {type(error).__name__}", file=sys.stderr)
        raise
