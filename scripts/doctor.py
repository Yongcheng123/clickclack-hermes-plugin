#!/usr/bin/env python3
"""Read-only ClickClack connectivity checks for an installed plugin.

This script intentionally uses only the Python standard library so it can run
outside the Hermes virtual environment.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only doctor for clickclack-hermes"
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--channel-id", required=True)
    return parser


class DoctorError(RuntimeError):
    pass


def _request(
    base_url: str,
    token: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/" + path.lstrip("/")
    if params:
        url += "?" + urlencode(params)
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "clickclack-hermes-doctor/0.1.0",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310
            payload = json.load(response)
    except HTTPError as exc:
        raise DoctorError(f"ClickClack HTTP {exc.code}") from exc
    except URLError as exc:
        reason = str(exc.reason).replace(token, "[REDACTED]")
        raise DoctorError(f"network error: {reason}") from exc
    except (OSError, ValueError) as exc:
        detail = str(exc).replace(token, "[REDACTED]")
        raise DoctorError(f"invalid response: {detail}") from exc
    if not isinstance(payload, dict):
        raise DoctorError("ClickClack returned an unexpected response")
    return payload


def _run(args: argparse.Namespace) -> int:
    token = os.getenv("CLICKCLACK_BOT_TOKEN", "").strip()
    if not token:
        print("FAIL token: CLICKCLACK_BOT_TOKEN is not available")
        return 2

    try:
        bot = _request(args.base_url, token, "/api/me").get("user") or {}
        if bot.get("kind") != "bot" or not bot.get("id") or not bot.get("handle"):
            print("FAIL identity: token does not resolve to a ClickClack bot")
            return 3
        print(
            f"PASS identity: @{bot['handle']} ({bot['id']}, "
            f"{'user-owned' if bot.get('owner_user_id') else 'service'})"
        )

        workspace = (
            _request(
                args.base_url,
                token,
                f"/api/workspaces/{args.workspace_id}",
            ).get("workspace")
            or {}
        )
        if workspace.get("id") != args.workspace_id:
            print("FAIL workspace: configured workspace is unavailable")
            return 4
        print(
            f"PASS workspace: {workspace.get('name') or workspace['id']} "
            f"({workspace['id']})"
        )

        channels = _request(
            args.base_url,
            token,
            f"/api/workspaces/{args.workspace_id}/channels",
        ).get("channels")
        if not isinstance(channels, list):
            channels = []
        channel = next(
            (item for item in channels if item.get("id") == args.channel_id),
            None,
        )
        if channel is None:
            print("FAIL channel: configured channel is unavailable to the bot")
            return 5
        print(
            f"PASS channel: "
            f"{channel.get('display_title') or channel.get('name') or channel['id']} "
            f"({channel['id']})"
        )

        page = _request(
            args.base_url,
            token,
            "/api/realtime/events",
            params={
                "workspace_id": args.workspace_id,
                "limit": 1,
                "include_tail": "true",
            },
        )
        if "events" not in page:
            print("FAIL realtime: durable event endpoint returned no events field")
            return 6
        print("PASS realtime: durable event endpoint is accessible")
        print("PASS doctor: configuration is ready for gateway startup")
        return 0
    except DoctorError as exc:
        print(f"FAIL clickclack: {exc}")
        return 10


def main() -> int:
    return _run(_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
