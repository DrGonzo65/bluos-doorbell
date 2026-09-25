"""Endpoint authorisation.

The service sits on a LAN and reports what music is playing in which room, so
who can read what is a real question. These tests pin the answer.

Run: python -m tests.test_auth
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import main as appmain  # noqa: E402
from app.config import Config  # noqa: E402
from app.discovery import PlayerRegistry  # noqa: E402
from app.orchestrator import DoorbellOrchestrator  # noqa: E402

PASS, FAIL = "  \033[32mPASS\033[0m", "  \033[31mFAIL\033[0m"
failures: list[str] = []

TOKEN = "s3cret-token-value"


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"{PASS} {label}")
    else:
        print(f"{FAIL} {label}" + (f" — {detail}" if detail else ""))
        failures.append(label)


def client_with(token: str) -> TestClient:
    """A client over the real app, with state wired up by hand (no lifespan)."""
    cfg = Config.model_validate({
        "new_room_enabled": True,
        "webhook": {"token": token},
        "discovery": {"auto": True},
    })
    http = httpx.AsyncClient()
    registry = PlayerRegistry(cfg, http)
    registry.note("192.168.1.51", "Kitchen", "PowerNode", "lsdp")
    appmain.state["config"] = cfg
    appmain.state["client"] = http
    appmain.state["registry"] = registry
    appmain.state["orchestrator"] = DoorbellOrchestrator(cfg, http, registry)
    return TestClient(appmain.app)


def test_protected_endpoints():
    print("\n1. Endpoints that must refuse an unauthenticated caller")
    c = client_with(TOKEN)
    for path in ("/inspect", "/discover"):
        check(f"GET {path} without a token -> 401",
              c.get(path).status_code == 401, str(c.get(path).status_code))
        check(f"GET {path} with a WRONG token -> 401",
              c.get(path, params={"token": "wrong"}).status_code == 401)
    check("POST /test/chime without a token -> 401",
          c.post("/test/chime").status_code == 401)
    check("POST /doorbell without a token -> 401",
          c.post("/doorbell").status_code == 401)


def test_health_leaks_nothing_anonymously():
    print("\n2. /health stays open but gives nothing away")
    c = client_with(TOKEN)
    r = c.get("/health")
    check("reachable without a token (Unraid WebUI + healthcheck need this)",
          r.status_code == 200, str(r.status_code))
    body = r.json()
    check("reports status", "status" in body)
    check("reports a build stamp", "build" in body)
    check("player NAMES withheld", "zone_names" not in body, str(list(body)))
    check("discovery detail withheld", "discovery" not in body, str(list(body)))
    check("last ring withheld", "last_result" not in body, str(list(body)))
    check("chime url withheld", "chime_url" not in body, str(list(body)))

    blob = str(body)
    check("no player name anywhere in the response", "Kitchen" not in blob, blob)
    check("no LAN address anywhere in the response", "192.168" not in blob, blob)
    check("says how to get more", "detail" in body)


def test_health_with_token_is_full():
    print("\n3. /health with the token still gives the full picture")
    c = client_with(TOKEN)
    body = c.get("/health", params={"token": TOKEN}).json()
    check("player names present", body.get("zone_names") == ["Kitchen"],
          str(body.get("zone_names")))
    check("discovery detail present", body.get("discovery") is not None)
    check("chime url present", "chime_url" in body)


def test_header_token_works():
    print("\n4. The token may travel in a header instead of the query string")
    c = client_with(TOKEN)
    r = c.get("/inspect", headers={"X-Doorbell-Token": TOKEN})
    check("X-Doorbell-Token accepted on /inspect", r.status_code == 200,
          str(r.status_code))
    body = c.get("/health", headers={"X-Doorbell-Token": TOKEN}).json()
    check("X-Doorbell-Token unlocks /health detail", "zone_names" in body)


def test_blank_token_is_wide_open_by_design():
    print("\n5. A blank token disables the check — deliberate, and warned about")
    c = client_with("")
    check("/inspect reachable with no token configured",
          c.get("/inspect").status_code == 200)
    check("/health gives no detail even so — it needs a real token",
          "zone_names" not in c.get("/health").json())
    check("'' counts as a default token, so startup warns",
          "" in appmain.DEFAULT_TOKENS)


def test_default_token_is_recognised():
    print("\n6. The shipped placeholder is recognised as no protection")
    check("'change-me' is in DEFAULT_TOKENS",
          "change-me" in appmain.DEFAULT_TOKENS)
    # It still functions as a token — the warning is what tells you to change it.
    c = client_with("change-me")
    check("it does still gate /inspect",
          c.get("/inspect").status_code == 401)
    check("...and works when supplied",
          c.get("/inspect", params={"token": "change-me"}).status_code == 200)


def test_comparison_is_constant_time():
    print("\n7. Token comparison uses a constant-time compare")
    import inspect
    src = inspect.getsource(appmain._token_ok)
    check("uses hmac.compare_digest", "compare_digest" in src)
    check("no plain == against the configured token",
          "== expected" not in src and "!= expected" not in src, src)


def main() -> int:
    test_protected_endpoints()
    test_health_leaks_nothing_anonymously()
    test_health_with_token_is_full()
    test_header_token_works()
    test_blank_token_is_wide_open_by_design()
    test_default_token_is_recognised()
    test_comparison_is_constant_time()

    print("\n" + "=" * 60)
    if failures:
        print(f"\033[31m{len(failures)} check(s) failed:\033[0m")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\033[32mAll checks passed.\033[0m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
