"""Create or update the Render web service and deploy it.

Uses the Render REST API (https://api.render.com/v1). Reads RENDER_API_KEY and
GEMINI_API_KEY from the environment or the repo's `.env`. Secrets are never printed.

    uv run python scripts/deploy_render.py --dry-run   # show payloads (secrets redacted)
    uv run python scripts/deploy_render.py             # create/update, deploy, wait, check /healthz

Prerequisites (one-time, in the Render dashboard): a Render account, an API key
(Account settings -> API keys) and the GitHub repo connected to Render
(Render's GitHub app must have access to the repository).

API reference: https://api-docs.render.com
  GET  /owners                          -> [{owner: {id, name, type}, cursor}]
  GET  /services?name=..&type=web_service
  POST /services                        -> {service, deployId}
  PATCH /services/{id}
  PUT  /services/{id}/env-vars          (replaces the full list)
  POST /services/{id}/deploys           -> {id, status}
  GET  /services/{id}/deploys/{deployId}
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

API = "https://api.render.com/v1"
ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "name": "voice-crm-agent",
    "repo": "https://github.com/nguyenduykhoa2601/voice-crm-agent",
    "branch": "main",
    "region": "singapore",
    "plan": "free",
}
SECRET_KEYS = {"GEMINI_API_KEY"}
DEPLOY_DONE_OK = {"live"}
DEPLOY_DONE_FAIL = {"build_failed", "update_failed", "canceled", "deactivated", "pre_deploy_failed"}


# --------------------------------------------------------------------------- helpers
def load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def env(name: str, dotenv: dict[str, str], default: str = "") -> str:
    return (os.getenv(name) or dotenv.get(name) or default).strip()


def redact(obj: Any) -> Any:
    if isinstance(obj, list):
        return [redact(item) for item in obj]
    if isinstance(obj, dict):
        if obj.get("key") in SECRET_KEYS and "value" in obj:
            return {**obj, "value": "***redacted***" if obj["value"] else "(empty)"}
        return {k: redact(v) for k, v in obj.items()}
    return obj


def show(title: str, payload: Any) -> None:
    print(f"--- {title}")
    print(json.dumps(redact(payload), indent=2))


class RenderApi:
    def __init__(self, token: str) -> None:
        self.token = token

    def call(self, method: str, path: str, body: Any = None, query: dict | None = None) -> Any:
        url = API + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=30) as res:
                    raw = res.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:500]
                if exc.code == 429 and attempt < 3:
                    time.sleep(5 * (attempt + 1))
                    continue
                sys.exit(f"Render API {method} {path} failed: HTTP {exc.code}: {detail}")
            except urllib.error.URLError as exc:
                if attempt < 3:
                    time.sleep(3)
                    continue
                sys.exit(f"Render API {method} {path} failed: {exc}")
        return None


# --------------------------------------------------------------------------- payloads
def env_vars(gemini_key: str, live_model: str) -> list[dict[str, str]]:
    return [
        {"key": "GEMINI_API_KEY", "value": gemini_key},
        {"key": "GEMINI_LIVE_MODEL", "value": live_model},
        {"key": "RENDER", "value": "1"},
        {"key": "MAX_SESSIONS", "value": "1"},
        {"key": "LOW_MEMORY", "value": "1"},
        {"key": "HEADLESS", "value": "1"},
    ]


def service_details(cfg: dict[str, str], include_region: bool) -> dict[str, Any]:
    details: dict[str, Any] = {
        "runtime": "docker",
        "plan": cfg["plan"],
        "healthCheckPath": "/healthz",
        "envSpecificDetails": {"dockerfilePath": "./Dockerfile", "dockerContext": "."},
    }
    if include_region:  # region can only be set at creation time
        details["region"] = cfg["region"]
        details["numInstances"] = 1
    return details


def create_payload(cfg: dict[str, str], owner_id: str, vars_: list[dict]) -> dict[str, Any]:
    return {
        "type": "web_service",
        "name": cfg["name"],
        "ownerId": owner_id,
        "repo": cfg["repo"],
        "branch": cfg["branch"],
        "autoDeployTrigger": "commit",
        "envVars": vars_,
        "serviceDetails": service_details(cfg, include_region=True),
    }


def update_payload(cfg: dict[str, str]) -> dict[str, Any]:
    return {
        "repo": cfg["repo"],
        "branch": cfg["branch"],
        "autoDeployTrigger": "commit",
        "serviceDetails": service_details(cfg, include_region=False),
    }


# --------------------------------------------------------------------------- flow
def pick_owner(api: RenderApi, wanted: str) -> str:
    owners = [item["owner"] for item in api.call("GET", "/owners", query={"limit": 20}) or []]
    if not owners:
        sys.exit("No Render workspace found for this API key.")
    if wanted:
        for owner in owners:
            if wanted in (owner["id"], owner.get("name"), owner.get("email")):
                return owner["id"]
        sys.exit(f"Workspace {wanted!r} not found; available: {[o.get('name') for o in owners]}")
    if len(owners) > 1:
        print(f"Several workspaces found, using the first ({owners[0].get('name')}). "
              "Pass --owner to choose.")
    return owners[0]["id"]


def find_service(api: RenderApi, name: str, owner_id: str) -> dict | None:
    items = api.call("GET", "/services", query={
        "name": name, "type": "web_service", "ownerId": owner_id, "limit": 20}) or []
    for item in items:
        if item["service"]["name"] == name:
            return item["service"]
    return None


def wait_for_deploy(api: RenderApi, service_id: str, deploy_id: str, timeout_s: int) -> str:
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        deploy = api.call("GET", f"/services/{service_id}/deploys/{deploy_id}") or {}
        status = deploy.get("status", "unknown")
        if status != last:
            print(f"  deploy {deploy_id}: {status}")
            last = status
        if status in DEPLOY_DONE_OK or status in DEPLOY_DONE_FAIL:
            return status
        time.sleep(10)
    return "timeout"


def check_health(url: str, attempts: int = 12) -> bool:
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/healthz", timeout=60) as res:
                body = json.loads(res.read() or b"{}")
                print(f"  /healthz -> {res.status} {json.dumps(body)}")
                return bool(body.get("ok"))
        except Exception as exc:  # noqa: BLE001 - cold start may take a while
            print(f"  /healthz attempt {attempt + 1}: {exc}")
            time.sleep(10)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="print payloads, call nothing")
    parser.add_argument("--name", default=DEFAULTS["name"])
    parser.add_argument("--repo", default=DEFAULTS["repo"])
    parser.add_argument("--branch", default=DEFAULTS["branch"])
    parser.add_argument("--region", default=DEFAULTS["region"])
    parser.add_argument("--plan", default=DEFAULTS["plan"])
    parser.add_argument("--owner", default="", help="workspace id/name/email (default: first)")
    parser.add_argument("--clear-cache", action="store_true")
    parser.add_argument("--timeout", type=int, default=1200, help="deploy wait, seconds")
    args = parser.parse_args()

    dotenv = load_dotenv(ROOT / ".env")
    token = env("RENDER_API_KEY", dotenv)
    gemini_key = env("GEMINI_API_KEY", dotenv) or env("GOOGLE_API_KEY", dotenv)
    live_model = env("GEMINI_LIVE_MODEL", dotenv, "gemini-3.1-flash-live-preview")
    cfg = {k: getattr(args, k) for k in DEFAULTS}
    vars_ = env_vars(gemini_key, live_model)
    deploy_body = {"clearCache": "clear" if args.clear_cache else "do_not_clear"}

    if args.dry_run:
        print(f"RENDER_API_KEY: {'set' if token else 'MISSING'}; "
              f"GEMINI_API_KEY: {'set' if gemini_key else 'MISSING'}")
        show("POST /services (if the service does not exist)",
             create_payload(cfg, "<ownerId from GET /owners>", vars_))
        show("PATCH /services/{id} (if it exists)", update_payload(cfg))
        show("PUT /services/{id}/env-vars (if it exists)", vars_)
        show("POST /services/{id}/deploys", deploy_body)
        return 0

    if not token:
        sys.exit("RENDER_API_KEY is not set (add it to .env).")
    if not gemini_key:
        sys.exit("GEMINI_API_KEY is not set (add it to .env).")

    api = RenderApi(token)
    owner_id = pick_owner(api, args.owner)
    service = find_service(api, cfg["name"], owner_id)

    if service is None:
        print(f"Creating web service {cfg['name']!r} ({cfg['plan']}, {cfg['region']}) ...")
        created = api.call("POST", "/services", create_payload(cfg, owner_id, vars_)) or {}
        service = created["service"]
        deploy_id = created.get("deployId")
        if not deploy_id:  # creation normally starts the first deploy
            deploy_id = (api.call("POST", f"/services/{service['id']}/deploys", deploy_body) or {})["id"]
    else:
        print(f"Updating existing service {service['id']} ...")
        api.call("PATCH", f"/services/{service['id']}", update_payload(cfg))
        api.call("PUT", f"/services/{service['id']}/env-vars", vars_)
        deploy_id = (api.call("POST", f"/services/{service['id']}/deploys", deploy_body) or {})["id"]

    service_id = service["id"]
    print(f"Dashboard: {service.get('dashboardUrl', '')}")
    print(f"Waiting for deploy {deploy_id} (free builds can take 5-15 min) ...")
    status = wait_for_deploy(api, service_id, deploy_id, args.timeout)
    if status != "live":
        print(f"Deploy ended with status {status!r}; check the dashboard logs.")
        return 1

    service = api.call("GET", f"/services/{service_id}") or service
    url = (service.get("serviceDetails") or {}).get("url", "")
    print(f"Service URL: {url}")
    if not url:
        return 1
    return 0 if check_health(url) else 1


if __name__ == "__main__":
    sys.exit(main())
