"""
One-time setup for autoresearch collaborative mode on Wizwand Swarm.

Examples:
  python3 setup_swarm.py --name phoenix --first-party
  python3 setup_swarm.py --name phoenix --first-party --smoke
  python3 setup_swarm.py --api-key swarm_xxx --smoke
"""

from __future__ import annotations

import argparse
import json
import os
import random
import string
import time
from pathlib import Path
from typing import Any, Optional

from coordinator import API_BASE_URL, KEY_FILE, Coordinator, SwarmApiError, swarm_request


def _random_suffix(length: int = 6) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))


def register_agent(name: str, first_party: bool, base_url: str) -> dict[str, Any]:
    payload = swarm_request(
        method="POST",
        path="/agents/register",
        api_key=None,
        body={"name": name, "firstParty": first_party},
        base_url=base_url,
    )
    agent = payload.get("agent")
    if not isinstance(agent, dict):
        raise SwarmApiError(502, "Invalid register response: missing agent object")
    return agent


def save_key(api_key: str, key_file: str) -> None:
    Path(key_file).write_text(api_key.strip() + "\n", encoding="utf-8")


def run_smoke(coord: Coordinator) -> None:
    desc = f"smoke test {int(time.time())}"
    exp_key = coord.claim_experiment(desc)
    if not exp_key:
        desc = f"smoke test {int(time.time())} {_random_suffix(4)}"
        exp_key = coord.claim_experiment(desc)

    if not exp_key:
        raise RuntimeError("Failed to claim smoke experiment after retries")

    train_py = ""
    if os.path.exists("train.py"):
        train_py = Path("train.py").read_text(encoding="utf-8")

    coord.publish_result(
        experiment_key=exp_key,
        val_bpb=1.2345,
        memory_gb=0.1,
        status="discard",
        description=desc,
        train_py_source=train_py,
        extra_metrics={"smoke": True},
    )

    coord.post_insight(
        "Smoke run succeeded: claim/result/analysis path on wizwand backend is healthy.",
        evidence_keys=[],
    )
    coord.publish_hypothesis(
        title="Smoke follow-up",
        hypothesis="Backend accepted the collaborative protocol payloads. Real training metrics can now be posted.",
        suggested_config={"smoke": False},
        evidence_keys=[],
        priority=1,
    )

    analysis = coord.analyze_swarm()
    print("\nSmoke run summary")
    print("-" * 60)
    print(f"total_results: {analysis.get('total_results')}")
    print(f"total_insights: {analysis.get('total_insights')}")
    print(f"active_claims: {len(analysis.get('active_claims', []))}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Set up autoresearch collaborative mode on Wizwand Swarm"
    )
    parser.add_argument(
        "--base-url",
        default=API_BASE_URL,
        help=f"Swarm API base URL (default: {API_BASE_URL})",
    )
    parser.add_argument(
        "--name",
        default="",
        help="Agent codename to register (letters/numbers/underscore, 2-32 chars)",
    )
    parser.add_argument(
        "--api-key",
        default="",
        help="Use an existing API key instead of registering",
    )
    parser.add_argument(
        "--first-party",
        action="store_true",
        default=False,
        help="Register as first-party so claim verification is not required",
    )
    parser.add_argument(
        "--save-key-file",
        default=KEY_FILE,
        help=f"Where to write API key (default: {KEY_FILE})",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Run claim/result/hypothesis/insight smoke test after setup",
    )
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")

    api_key: Optional[str] = args.api_key.strip() or None

    if not api_key:
        if not args.name:
            generated_name = f"swarm_{int(time.time())}_{_random_suffix(4)}"
            print(f"No --name provided, using generated name: {generated_name}")
            args.name = generated_name

        print(f"Registering agent '{args.name}' at {base_url} ...")
        agent = register_agent(args.name, first_party=args.first_party, base_url=base_url)
        api_key = str(agent.get("api_key") or "").strip()
        if not api_key:
            raise RuntimeError("Register succeeded but api_key missing in response")

        save_key(api_key, args.save_key_file)
        print(f"Saved API key to {args.save_key_file}")

        claim_url = agent.get("claim_url")
        verification_code = agent.get("verification_code")
        if claim_url:
            print(f"claim_url: {claim_url}")
        if verification_code:
            print(f"verification_code: {verification_code}")

        if not args.first_party:
            print(
                "NOTE: non-first-party agents must complete claim verification before write APIs are allowed."
            )

    coord = Coordinator(api_key=api_key, api_base_url=base_url)
    if not coord.test_connectivity():
        raise RuntimeError("Connectivity test failed. Check API base URL and API key.")

    me = coord.refresh_identity() or {}
    print("Connected as:")
    print(json.dumps(me, indent=2))

    coord.announce()

    if args.smoke:
        run_smoke(coord)


if __name__ == "__main__":
    main()
