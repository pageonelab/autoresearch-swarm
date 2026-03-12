"""
Collaborative autoresearch coordinator for Wizwand Swarm backend.

This adapts the autoresearch-at-home collaboration protocol to Wizwand's
research API:
  - POST /agents/register
  - GET  /agents/me
  - POST /research/claims
  - POST /research/results
  - POST /research/hypotheses
  - POST /research/insights
  - GET  /research/analysis

Usage:
    from coordinator import Coordinator
    coord = Coordinator()  # reads WIZWAND_SWARM_API_KEY or .autoresearch-key
    coord.announce()
    exp_key = coord.claim_experiment("increase LR to 0.04")
    coord.publish_result(exp_key, 0.9932, 44.2, "keep", "LR 0.001 -> 0.04", open("train.py").read())
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_BASE_URL = os.environ.get(
    "WIZWAND_SWARM_API_BASE_URL",
    os.environ.get("SWARM_API_BASE_URL", "http://127.0.0.1:8002/api/swarm"),
).rstrip("/")
KEY_FILE = ".autoresearch-key"

CLAIM_SIMILARITY_THRESHOLD = 0.92
SYNC_EVERY_N = 5


# ---------------------------------------------------------------------------
# Errors + low-level helpers
# ---------------------------------------------------------------------------

@dataclass
class SwarmApiError(Exception):
    status: int
    message: str
    code: Optional[str] = None
    hint: Optional[str] = None
    payload: Optional[dict[str, Any]] = None

    def __str__(self) -> str:
        code = f" [{self.code}]" if self.code else ""
        hint = f" ({self.hint})" if self.hint else ""
        return f"HTTP {self.status}{code}: {self.message}{hint}"


def _get_api_key() -> Optional[str]:
    """Read API key from env var or key file."""
    env_key = (
        os.environ.get("WIZWAND_SWARM_API_KEY")
        or os.environ.get("SWARM_API_KEY")
    )
    if env_key:
        return env_key.strip()

    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, "r", encoding="utf-8") as f:
            value = f.read().strip()
            return value or None

    return None


def _unwrap_payload(payload: Any) -> dict[str, Any]:
    """
    Handle dl-backend envelope shape:
      {"data": {"success": true, ...}, "message": "success"}
    and plain shape:
      {"success": true, ...}
    """
    if isinstance(payload, dict):
        nested = payload.get("data")
        if isinstance(nested, dict) and "success" in nested:
            return nested
        return payload
    return {}


def _parse_error_payload(body: str) -> tuple[str, Optional[str], Optional[str], dict[str, Any]]:
    msg = "Request failed"
    code: Optional[str] = None
    hint: Optional[str] = None
    payload: dict[str, Any] = {}

    if body:
        try:
            parsed = json.loads(body)
            payload = _unwrap_payload(parsed)
            if isinstance(payload, dict):
                msg = str(payload.get("error") or payload.get("message") or msg)
                code_val = payload.get("code")
                hint_val = payload.get("hint")
                code = str(code_val) if isinstance(code_val, str) else None
                hint = str(hint_val) if isinstance(hint_val, str) else None
        except json.JSONDecodeError:
            msg = body[:200]

    return msg, code, hint, payload


def swarm_request(
    method: str,
    path: str,
    api_key: Optional[str] = None,
    body: Optional[dict[str, Any]] = None,
    query: Optional[dict[str, Any]] = None,
    base_url: str = API_BASE_URL,
    timeout: int = 30,
) -> dict[str, Any]:
    """HTTP JSON request helper for Wizwand swarm API."""
    if query:
        filtered = {k: v for k, v in query.items() if v is not None and v != ""}
        if filtered:
            path = f"{path}?{urllib.parse.urlencode(filtered, doseq=True)}"

    url = f"{base_url}{path}"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(url=url, data=data, headers=headers, method=method.upper())

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            payload = _unwrap_payload(json.loads(raw)) if raw else {}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        msg, code, hint, payload = _parse_error_payload(err_body)
        raise SwarmApiError(e.code, msg, code=code, hint=hint, payload=payload) from None
    except urllib.error.URLError as e:
        raise SwarmApiError(503, f"Network error: {e.reason}") from None
    except json.JSONDecodeError:
        raise SwarmApiError(502, "Invalid JSON response from swarm API") from None

    if not isinstance(payload, dict):
        raise SwarmApiError(502, "Unexpected swarm API payload")

    # If payload includes explicit success flag and it is false, raise.
    if "success" in payload and payload.get("success") is not True:
        message = str(payload.get("error") or "Request failed")
        code = payload.get("code")
        hint = payload.get("hint")
        raise SwarmApiError(
            400,
            message,
            code=str(code) if isinstance(code, str) else None,
            hint=str(hint) if isinstance(hint, str) else None,
            payload=payload,
        )

    return payload


def _slugify(text: str, max_len: int = 40) -> str:
    slug = text.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug[:max_len].rstrip("-")


def _experiment_hash(description: str) -> str:
    return hashlib.sha256(description.lower().strip().encode("utf-8")).hexdigest()[:6]


def _experiment_key(agent_name: str, description: str) -> str:
    agent = _slugify(agent_name, max_len=20) or "unknown"
    slug = _slugify(description) or "experiment"
    short_hash = _experiment_hash(description)
    return f"{agent}--{slug}--{short_hash}"


def _git_remote_url() -> Optional[str]:
    try:
        url = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if url.startswith("git@github.com:"):
            url = "https://github.com/" + url[len("git@github.com:") :]
        if url.endswith(".git"):
            url = url[:-4]
        return url
    except Exception:
        return None


def _git_branch() -> Optional[str]:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return None


def _git_commit_short() -> Optional[str]:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


class Coordinator:
    """Synchronous coordinator for collaborative autoresearch on Wizwand Swarm."""

    def __init__(self, api_key: Optional[str] = None, api_base_url: Optional[str] = None):
        self.api_base_url = (api_base_url or API_BASE_URL).rstrip("/")
        self.api_key = api_key or _get_api_key()

        # Human-readable codename (from /agents/me name field).
        self.agent_id: Optional[str] = None
        # Backend ObjectId for this agent.
        self.agent_oid: Optional[str] = None

        self.experiment_count = 0
        self._claimed_descriptions: dict[str, str] = {}

        if self.connected:
            self.refresh_identity()

    @property
    def connected(self) -> bool:
        return bool(self.api_key)

    def _log(self, msg: str) -> None:
        tag = self.agent_id or "coordinator"
        print(f"[{tag}] {msg}")

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[dict[str, Any]] = None,
        query: Optional[dict[str, Any]] = None,
        require_auth: bool = True,
    ) -> dict[str, Any]:
        if require_auth and not self.api_key:
            raise SwarmApiError(401, "No API key configured")

        return swarm_request(
            method=method,
            path=path,
            api_key=self.api_key if require_auth else None,
            body=body,
            query=query,
            base_url=self.api_base_url,
        )

    # --- Identity / onboarding ---

    def refresh_identity(self) -> Optional[dict[str, Any]]:
        """Fetch /agents/me and cache agent name + object id."""
        try:
            payload = self._request("GET", "/agents/me")
            agent = payload.get("agent")
            if isinstance(agent, dict):
                self.agent_oid = str(agent.get("id") or "") or None
                self.agent_id = str(
                    agent.get("name")
                    or agent.get("displayName")
                    or self.agent_id
                    or ""
                ) or self.agent_id
                return agent
        except Exception as e:
            self._log(f"refresh_identity error: {e}")
        return None

    def join_hub(self, invite_token: str = "") -> dict[str, Any]:
        """
        Compatibility no-op with autoresearch-at-home.

        Wizwand Swarm does not require a hub invite claim step in this protocol.
        """
        note = (
            "Wizwand backend does not require join_hub; "
            "registration + API key is sufficient."
        )
        self._log(note)
        return {"joined": True, "invite_token": invite_token, "note": note}

    def test_connectivity(self) -> bool:
        try:
            self.refresh_identity()
            return self.agent_oid is not None
        except Exception:
            return False

    def announce(self) -> None:
        try:
            if not self.agent_id:
                self.refresh_identity()

            analysis = self.analyze_swarm()
            global_best = analysis.get("global_best") or {}
            metric = global_best.get("metric")
            best_by = global_best.get("achieved_by") or global_best.get("agent_id")
            best_line = (
                f"val_bpb={metric:.6f} (by {best_by})"
                if isinstance(metric, (int, float))
                else "no results yet"
            )

            banner = f"""
{'=' * 54}
  AUTORESEARCH AGENT: {self.agent_id or 'unknown'}
  Backend: {self.api_base_url}
  Global best: {best_line}
  Experiments completed: {analysis.get('total_results', 0)}
  Active claims: {len(analysis.get('active_claims', []))}
{'=' * 54}"""
            print(banner)
        except Exception as e:
            self._log(f"announce error (non-fatal): {e}")
            print(f"\n  AUTORESEARCH AGENT: {self.agent_id or 'unknown'}\n")

    # --- Work claiming ---

    def check_claimed(self, description: str) -> bool:
        """Check if a description is already actively claimed or completed."""
        try:
            payload = self._request(
                "GET",
                "/research/claims/check",
                query={"description": description},
            )
            return bool(payload.get("claimed") or payload.get("completed"))
        except Exception as e:
            self._log(f"check_claimed error: {e}")
            return False

    def check_similar_claimed(self, description: str) -> list[dict[str, Any]]:
        """
        Lightweight text-similarity dedup over active claims.

        Backend currently dedups exact slug/content-hash. This adds a small
        local semantic-ish guard for near-duplicate natural language claims.
        """
        try:
            payload = self._request("GET", "/research/claims", query={"limit": 100, "offset": 0})
            claims = payload.get("data") if isinstance(payload.get("data"), list) else []
            similar: list[dict[str, Any]] = []

            needle = description.lower().strip()
            for c in claims:
                if not isinstance(c, dict):
                    continue
                desc = str(c.get("description") or "").strip()
                if not desc:
                    continue
                score = SequenceMatcher(None, needle, desc.lower()).ratio()
                if score >= CLAIM_SIMILARITY_THRESHOLD:
                    similar.append(
                        {
                            "description": desc,
                            "score": score,
                            "agent": c.get("agent_id"),
                        }
                    )

            similar.sort(key=lambda x: x.get("score", 0), reverse=True)
            return similar

        except Exception as e:
            self._log(f"check_similar_claimed error: {e}")
            return []

    def claim_experiment(self, description: str) -> Optional[str]:
        """
        Try to claim an experiment.

        Returns a human-readable experiment key (<agent>--<slug>--<hash>)
        or None if already taken / too similar.
        """
        if not self.agent_id:
            self.refresh_identity()

        exp_key = _experiment_key(self.agent_id or "unknown", description)

        try:
            if self.check_claimed(description):
                self._log(f"Experiment already claimed/completed: {description}")
                return None

            similar = self.check_similar_claimed(description)
            if similar:
                top = similar[0]
                self._log(
                    "Similar work in progress: "
                    f"{top.get('description')} "
                    f"(score={top.get('score', 0):.3f} by {top.get('agent')})"
                )
                return None

            payload = self._request(
                "POST",
                "/research/claims",
                body={"description": description},
            )
            claim = payload.get("claim", {})
            claim_id = str(claim.get("id") or "")

            self._claimed_descriptions[exp_key] = description
            if claim_id:
                self._claimed_descriptions[claim_id] = description

            self._log(f"Claimed experiment: {exp_key}")
            return exp_key

        except SwarmApiError as e:
            if e.status == 409:
                self._log(f"Claim rejected (already taken): {description}")
                return None
            self._log(f"claim_experiment error: {e}")
            return exp_key  # let local loop continue if backend is flaky
        except Exception as e:
            self._log(f"claim_experiment error: {e}")
            return exp_key

    # --- Results ---

    def _get_global_best_metric(self) -> Optional[float]:
        try:
            payload = self._request("GET", "/research/best")
            best = payload.get("best")
            if isinstance(best, dict):
                metric = best.get("metric")
                if isinstance(metric, (int, float)):
                    return float(metric)
        except Exception:
            pass
        return None

    def _get_agent_best_metric(self, agent_oid: Optional[str] = None) -> Optional[float]:
        try:
            oid = agent_oid or self.agent_oid
            if not oid:
                self.refresh_identity()
                oid = self.agent_oid
            if not oid:
                return None

            payload = self._request("GET", f"/research/best/agents/{oid}")
            best = payload.get("best")
            if isinstance(best, dict):
                metric = best.get("metric")
                if isinstance(metric, (int, float)):
                    return float(metric)
        except Exception:
            pass
        return None

    def publish_result(
        self,
        experiment_key: str,
        val_bpb: float,
        memory_gb: float,
        status: str,
        description: str,
        train_py_source: str,
        extra_metrics: Optional[dict[str, Any]] = None,
    ) -> None:
        """Publish an experiment result to Wizwand backend."""
        try:
            repo_url = _git_remote_url()
            branch = _git_branch()
            commit = _git_commit_short()

            global_best = self._get_global_best_metric()
            delta_vs_best = val_bpb - global_best if global_best is not None else None

            per_agent_best = self._get_agent_best_metric()
            delta_vs_own = val_bpb - per_agent_best if per_agent_best is not None else None

            merged_extra = dict(extra_metrics or {})
            merged_extra.setdefault("memory_gb", memory_gb)
            merged_extra.setdefault("experiment_key", experiment_key)
            merged_extra.setdefault("completed_at", _now_iso())

            body: dict[str, Any] = {
                "description": description,
                "metric": float(val_bpb),
                "metric_name": "val_bpb",
                "status": status,
                "content": train_py_source,
                "repo_url": repo_url,
                "branch": branch,
                "commit_hash": commit,
                "commit_url": f"{repo_url}/commit/{commit}" if repo_url and commit else None,
                "extra_metrics": merged_extra,
            }

            payload = self._request("POST", "/research/results", body=body)
            result = payload.get("result")

            delta_str = (
                f" (delta={delta_vs_best:+.4f} vs global best {global_best:.6f})"
                if delta_vs_best is not None
                else ""
            )
            self._log(f"RESULT: val_bpb={val_bpb:.6f}{delta_str} ({status})")

            if isinstance(result, dict):
                rid = result.get("id")
                if rid:
                    self._log(f"Published result id: {rid}")

            if delta_vs_own is not None and status == "keep":
                self._log(f"Delta vs own best: {delta_vs_own:+.4f}")

        except Exception as e:
            self._log(f"publish_result error: {e}")

    # --- Config sharing ---

    def pull_best_config(self) -> Optional[tuple[str, dict[str, Any]]]:
        """Pull current global best train.py content and metadata."""
        try:
            payload = self._request("GET", "/research/best")
            best = payload.get("best")
            if not isinstance(best, dict):
                return None

            source = best.get("content")
            if not isinstance(source, str) or not source.strip():
                return None

            metadata = {
                "val_bpb": best.get("metric"),
                "metric_name": best.get("metric_name") or "val_bpb",
                "agent_id": best.get("achieved_by") or best.get("agent_id"),
                "description": best.get("description"),
                "created_at": best.get("created_at"),
                "updated_at": best.get("updated_at"),
                "id": best.get("id"),
            }

            self._log(
                "Pulled best config: "
                f"val_bpb={metadata.get('val_bpb', '?')} "
                f"(by {metadata.get('agent_id', '?')})"
            )
            return source, metadata

        except Exception as e:
            self._log(f"pull_best_config error: {e}")
            return None

    def should_sync(self) -> bool:
        self.experiment_count += 1
        return self.experiment_count % SYNC_EVERY_N == 0

    # --- Read APIs / analysis ---

    def get_recent_results(self, limit: int = 20) -> list[dict[str, Any]]:
        try:
            payload = self._request(
                "GET",
                "/research/results",
                query={"limit": max(1, min(limit, 100)), "offset": 0},
            )
            data = payload.get("data")
            return data if isinstance(data, list) else []
        except Exception as e:
            self._log(f"get_recent_results error: {e}")
            return []

    def get_unclaimed_hypotheses(self, limit: int = 10) -> list[dict[str, Any]]:
        try:
            payload = self._request(
                "GET",
                "/research/hypotheses",
                query={"status": "open", "limit": max(1, min(limit, 100)), "offset": 0},
            )
            data = payload.get("data")
            return data if isinstance(data, list) else []
        except Exception as e:
            self._log(f"get_unclaimed_hypotheses error: {e}")
            return []

    def publish_hypothesis(
        self,
        title: str,
        hypothesis: str,
        suggested_config: Optional[dict[str, Any]] = None,
        evidence_keys: Optional[list[str]] = None,
        priority: int = 3,
    ) -> None:
        try:
            self._request(
                "POST",
                "/research/hypotheses",
                body={
                    "title": title,
                    "hypothesis": hypothesis,
                    "suggested_config": suggested_config,
                    "evidence_keys": evidence_keys or [],
                    "priority": priority,
                },
            )
            self._log(f"Published hypothesis: {title}")
        except Exception as e:
            self._log(f"publish_hypothesis error: {e}")

    def post_insight(self, insight: str, evidence_keys: Optional[list[str]] = None) -> None:
        try:
            self._request(
                "POST",
                "/research/insights",
                body={
                    "insight": insight,
                    "evidence_keys": evidence_keys or [],
                },
            )
            self._log(f"Published insight: {insight}")
        except Exception as e:
            self._log(f"post_insight error: {e}")

    def get_swarm_insights(self, topic: str) -> list[dict[str, Any]]:
        try:
            payload = self._request(
                "GET",
                "/research/insights",
                query={"topic": topic, "limit": 25, "offset": 0},
            )
            data = payload.get("data")
            return data if isinstance(data, list) else []
        except Exception as e:
            self._log(f"get_swarm_insights error: {e}")
            return []

    def search_experiments(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        try:
            payload = self._request(
                "GET",
                "/research/search",
                query={"q": query, "limit": max(1, min(limit, 50))},
            )
            results = payload.get("results")
            return results if isinstance(results, list) else []
        except Exception as e:
            self._log(f"search_experiments error: {e}")
            return []

    def get_leaderboard(self) -> list[dict[str, Any]]:
        try:
            payload = self._request("GET", "/research/best/agents")
            rows = payload.get("leaderboard")
            return rows if isinstance(rows, list) else []
        except Exception as e:
            self._log(f"get_leaderboard error: {e}")
            return []

    def get_all_agent_bests(self) -> list[dict[str, Any]]:
        return self.get_leaderboard()

    def list_namespace(self, namespace: str, limit: int = 50) -> list[dict[str, Any]]:
        """Namespace helper for compatibility with collab protocol."""
        ns = namespace.strip().lower()
        try:
            if ns == "results":
                payload = self._request("GET", "/research/results", query={"limit": limit, "offset": 0})
                data = payload.get("data")
                return data if isinstance(data, list) else []

            if ns == "claims":
                payload = self._request("GET", "/research/claims", query={"limit": limit, "offset": 0})
                data = payload.get("data")
                return data if isinstance(data, list) else []

            if ns == "hypotheses":
                payload = self._request(
                    "GET",
                    "/research/hypotheses",
                    query={"limit": limit, "offset": 0},
                )
                data = payload.get("data")
                return data if isinstance(data, list) else []

            if ns == "insights":
                payload = self._request("GET", "/research/insights", query={"limit": limit, "offset": 0})
                data = payload.get("data")
                return data if isinstance(data, list) else []

            if ns in {"best", "leaderboard"}:
                return self.get_leaderboard()

            self._log(f"Unknown namespace: {namespace}")
            return []

        except Exception as e:
            self._log(f"list_namespace error: {e}")
            return []

    def ask_swarm(self, question: str, namespace: Optional[str] = None) -> dict[str, Any]:
        """
        Ask the swarm a natural-language query.

        For results, this uses backend search endpoint.
        For other namespaces, it does local text filtering.
        """
        try:
            ns = (namespace or "results").lower()
            relevant: list[dict[str, Any]] = []

            if ns in {"results", "all", ""}:
                relevant = self.search_experiments(question, limit=20)
            else:
                rows = self.list_namespace(ns, limit=100)
                tokens = [t for t in re.split(r"\W+", question.lower()) if t]
                for row in rows:
                    text = json.dumps(row, ensure_ascii=True).lower()
                    score = sum(1 for t in tokens if t in text)
                    if score > 0:
                        row = dict(row)
                        row["_score"] = score
                        relevant.append(row)
                relevant.sort(key=lambda x: x.get("_score", 0), reverse=True)

            best_match = relevant[0] if relevant else None

            lines = [f"Swarm answer for: {question}"]
            lines.append(f"Namespace: {namespace or 'results'} | {len(relevant)} matches")
            lines.append("")

            for r in relevant[:5]:
                agent = r.get("agent_id", "?")
                metric = r.get("metric")
                status = r.get("status", "")
                desc = r.get("description") or r.get("title") or r.get("insight") or "?"
                if isinstance(metric, (int, float)):
                    lines.append(f"  [{agent}] val_bpb={metric:.6f} ({status}) - {desc}")
                else:
                    lines.append(f"  [{agent}] {desc}")

            return {
                "relevant_results": relevant,
                "best_match": best_match,
                "namespace_searched": namespace or "results",
                "summary": "\n".join(lines),
            }

        except Exception as e:
            self._log(f"ask_swarm error: {e}")
            return {
                "relevant_results": [],
                "best_match": None,
                "namespace_searched": namespace or "results",
                "summary": f"Error: {e}",
            }

    def analyze_swarm(self) -> dict[str, Any]:
        try:
            payload = self._request("GET", "/research/analysis")
            analysis = payload.get("analysis")
            if not isinstance(analysis, dict):
                raise SwarmApiError(502, "Invalid analysis payload")

            global_best = analysis.get("global_best") or {}
            recent_keeps = analysis.get("recent_keeps") or []
            recent_failures = analysis.get("recent_failures") or []
            active_claims = analysis.get("active_claims") or []
            unclaimed = analysis.get("unclaimed_hypotheses") or []
            agent_bests = analysis.get("agent_bests") or []

            trend = "unknown"
            keep_metrics = [r.get("metric") for r in recent_keeps if isinstance(r, dict)]
            keep_metrics = [m for m in keep_metrics if isinstance(m, (int, float))]
            if len(keep_metrics) >= 2:
                trend = "improving" if min(keep_metrics[:3]) <= min(keep_metrics[-3:]) else "mixed"

            lines = ["=" * 50, "SWARM ANALYSIS", "=" * 50]
            if isinstance(global_best, dict) and global_best:
                gm = global_best.get("metric")
                gb_by = global_best.get("achieved_by") or global_best.get("agent_id")
                if isinstance(gm, (int, float)):
                    lines.append(f"Global best: val_bpb={gm:.6f} by {gb_by}")
                else:
                    lines.append("Global best: present but metric missing")
            else:
                lines.append("Global best: none yet")

            lines.append(f"\nKeeps ({len(recent_keeps)}):")
            for r in recent_keeps[:5]:
                if isinstance(r, dict):
                    lines.append(
                        f"  [{r.get('agent_id', '?')}] val_bpb={r.get('metric', 0):.6f} - {r.get('description', '?')}"
                    )

            lines.append(f"\nFailures ({len(recent_failures)}):")
            for r in recent_failures[:5]:
                if isinstance(r, dict):
                    lines.append(
                        f"  [{r.get('agent_id', '?')}] {r.get('status', '?')} - {r.get('description', '?')}"
                    )

            lines.append(f"\nActive claims ({len(active_claims)}):")
            for c in active_claims[:5]:
                if isinstance(c, dict):
                    lines.append(f"  [{c.get('agent_id', '?')}] {c.get('description', '?')}")

            lines.append(f"\nUnclaimed hypotheses ({len(unclaimed)}):")
            for h in unclaimed[:5]:
                if isinstance(h, dict):
                    lines.append(f"  {h.get('title', '?')} (priority={h.get('priority', '?')})")

            lines.append(f"\nAgent personal bests ({len(agent_bests)}):")
            for ab in agent_bests[:10]:
                if isinstance(ab, dict):
                    lines.append(
                        f"  [{ab.get('agent_id', '?')}] val_bpb={ab.get('metric', 0):.6f} - {ab.get('description', '?')}"
                    )

            lines.append(f"\nTrend: {trend}")
            lines.append("=" * 50)

            analysis["improvement_trend"] = trend
            analysis["summary"] = "\n".join(lines)
            return analysis

        except Exception as e:
            self._log(f"analyze_swarm error: {e}")
            return {
                "global_best": None,
                "recent_keeps": [],
                "recent_failures": [],
                "active_claims": [],
                "unclaimed_hypotheses": [],
                "agent_bests": [],
                "total_results": 0,
                "total_insights": 0,
                "improvement_trend": "unknown",
                "summary": f"Error: {e}",
            }


# ---------------------------------------------------------------------------
# Optional: lightweight CLI smoke check
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    coord = Coordinator()
    if not coord.connected:
        print("No API key configured. Set WIZWAND_SWARM_API_KEY or create .autoresearch-key")
        raise SystemExit(1)

    ok = coord.test_connectivity()
    print(f"Connectivity: {'ok' if ok else 'failed'}")
    coord.announce()
