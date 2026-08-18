#!/usr/bin/env python3
"""
git_rollback.py — the GitOps rollback step for your existing agent,
                  WITH a human-in-the-loop approval gate.

Flow (default, ROLLBACK_MODE=pr):
  1. Watch the deployment's pods after a deploy.
  2. If they don't reach Ready within TIMEOUT (ImagePullBackOff / ErrImagePull /
     CrashLoopBackOff / stuck not-Ready), treat the deploy as failed.
  3. Create a branch that reverts the bad deploy commit and OPEN A PULL REQUEST.
  4. A human reviews and merges the PR.  <-- approval gate
  5. Only after merge does ArgoCD (selfHeal) sync main and roll the cluster back.

Nothing reaches the cluster without a person merging the revert PR. The agent
does not merge on its own.

Why revert Git instead of `kubectl rollout undo`:
  With ArgoCD selfHeal=true, Git is the source of truth. A kubectl rollback would
  be reverted by ArgoCD on its next sync. The durable, reviewable fix is a commit.

Wire-in options:
  - Standalone:   python3 git_rollback.py
  - From your agent:
        from git_rollback import watch_and_rollback
        result = watch_and_rollback()
        # result is one of: "healthy", "pr_opened", "rolled_back", "error"

Config via environment variables (sensible defaults):
  NAMESPACE     default: monitoring
  SELECTOR      default: app=demo-app
  DEPLOYMENT    default: demo-app
  TIMEOUT       seconds to wait for health before rolling back (default: 120)
  POLL          seconds between checks (default: 10)
  REPO_DIR      path to a checkout of the GitOps repo (default: current dir)
  BRANCH        base branch ArgoCD tracks (default: main)

  ROLLBACK_MODE  "pr" (default, human approval) | "direct" (push straight to main)
  REVIEWERS      optional CSV of GitHub usernames to request review from
  WAIT_FOR_MERGE "1" to keep polling until the PR is merged (default: 0)
  MERGE_TIMEOUT  seconds to wait for a human to merge (default: 1800)

  GH_TOKEN      token that can push branches and open PRs (repo + PR write)
  GH_REPO       "<owner>/<repo>"; auto-derived from GH_REMOTE if omitted
  GH_REMOTE     https remote, e.g. https://github.com/<org>/<repo>.git
  SLACK_WEBHOOK optional; if set, the PR link is posted here for the approver
  DRY_RUN       "1" to do everything except push/open-PR (default: 0)
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime

NAMESPACE = os.getenv("NAMESPACE", "demoapp")
SELECTOR = os.getenv("SELECTOR", "app=my-app")
DEPLOYMENT = os.getenv("DEPLOYMENT", "demo-app")
TIMEOUT = int(os.getenv("TIMEOUT", "120"))
POLL = int(os.getenv("POLL", "10"))
REPO_DIR = os.getenv("REPO_DIR", os.getcwd())
BRANCH = os.getenv("BRANCH", "main")

ROLLBACK_MODE = os.getenv("ROLLBACK_MODE", "pr").lower()
REVIEWERS = [r.strip() for r in os.getenv("REVIEWERS", "").split(",") if r.strip()]
WAIT_FOR_MERGE = os.getenv("WAIT_FOR_MERGE", "0") == "1"
MERGE_TIMEOUT = int(os.getenv("MERGE_TIMEOUT", "1800"))

GH_TOKEN = os.getenv("GH_TOKEN", "")
GH_REMOTE = os.getenv("GH_REMOTE", "")
GH_REPO = os.getenv("GH_REPO", "")
SLACK_WEBHOOK = os.getenv("SLACK_WEBHOOK", "")
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"

BAD_WAITING_REASONS = {"ImagePullBackOff", "ErrImagePull", "CrashLoopBackOff", "InvalidImageName"}
GH_API = "https://api.github.com"


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", flush=True)


def run(cmd, cwd=None, check=True):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        log(f"command failed: {' '.join(cmd)}\n{p.stderr.strip()}", "ERROR")
    return p.returncode, p.stdout.strip(), p.stderr.strip()


# --------------------------------------------------------------------------- #
# Health evaluation
# --------------------------------------------------------------------------- #
def get_pods():
    rc, out, _ = run(
        ["kubectl", "get", "pods", "-n", NAMESPACE, "-l", SELECTOR, "-o", "json"],
        check=False,
    )
    if rc != 0 or not out:
        return []
    try:
        return json.loads(out).get("items", [])
    except json.JSONDecodeError:
        return []


def evaluate_health():
    """Return 'healthy', 'failing', or 'pending'."""
    pods = get_pods()
    if not pods:
        return "pending"
    all_ready = True
    for pod in pods:
        status = pod.get("status", {})
        css = status.get("containerStatuses", [])
        if not css:
            all_ready = False
        for cs in css:
            reason = cs.get("state", {}).get("waiting", {}).get("reason", "")
            if reason in BAD_WAITING_REASONS:
                return "failing"
            if not cs.get("ready", False):
                all_ready = False
    return "healthy" if all_ready else "pending"


# --------------------------------------------------------------------------- #
# Git helpers
# --------------------------------------------------------------------------- #
def bad_deploy_sha():
    run(["git", "fetch", "origin", BRANCH], cwd=REPO_DIR, check=False)
    rc, sha, _ = run(["git", "rev-parse", f"origin/{BRANCH}"], cwd=REPO_DIR, check=False)
    return sha if rc == 0 else ""


def commit_touched_manifest(sha):
    rc, out, _ = run(["git", "show", "--name-only", "--pretty=format:", sha],
                     cwd=REPO_DIR, check=False)
    return any(line.strip().startswith("k8s/") for line in out.splitlines())


def authed_url(base):
    if base and GH_TOKEN and base.startswith("https://"):
        return base.replace("https://", f"https://x-access-token:{GH_TOKEN}@")
    return base


def push_target():
    return authed_url(GH_REMOTE) if GH_REMOTE else "origin"


def derive_repo():
    """Return '<owner>/<repo>' from GH_REPO or GH_REMOTE."""
    if GH_REPO:
        return GH_REPO
    r = GH_REMOTE
    if not r:
        return ""
    r = r.rstrip("/")
    if r.endswith(".git"):
        r = r[:-4]
    if r.startswith("git@"):          # git@github.com:owner/repo
        r = r.split(":", 1)[-1]
    else:                              # https://github.com/owner/repo
        r = r.split("github.com/", 1)[-1]
    return r


# --------------------------------------------------------------------------- #
# GitHub API
# --------------------------------------------------------------------------- #
def gh_api(method, path, payload=None):
    url = f"{GH_API}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {GH_TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "rollback-agent")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode()
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode()}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": str(e)}


def notify_slack(text):
    if not SLACK_WEBHOOK:
        return
    try:
        req = urllib.request.Request(
            SLACK_WEBHOOK,
            data=json.dumps({"text": text}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:  # noqa: BLE001
        log(f"Slack notify failed (non-fatal): {e}", "WARNING")


# --------------------------------------------------------------------------- #
# Rollback paths
# --------------------------------------------------------------------------- #
def prepare_revert_branch(sha):
    """Create rollback/<sha> off origin/BRANCH with the revert committed. Returns branch name."""
    branch = f"rollback/{sha[:10]}"
    run(["git", "fetch", "origin", BRANCH], cwd=REPO_DIR, check=False)
    run(["git", "checkout", "-B", branch, f"origin/{BRANCH}"], cwd=REPO_DIR, check=False)
    run(["git", "config", "user.name", "rollback-agent"], cwd=REPO_DIR, check=False)
    run(["git", "config", "user.email", "rollback-agent@users.noreply.github.com"],
        cwd=REPO_DIR, check=False)
    rc, _, err = run(["git", "revert", "--no-edit", sha], cwd=REPO_DIR, check=False)
    if rc != 0:
        log(f"git revert failed: {err}", "ERROR")
        return None
    return branch


def rollback_via_pr(sha):
    """Human-in-the-loop: open a revert PR and stop. A person merges it."""
    repo = derive_repo()
    if not repo or not GH_TOKEN:
        log("ROLLBACK_MODE=pr needs GH_TOKEN and GH_REPO/GH_REMOTE set.", "ERROR")
        return "error"

    branch = prepare_revert_branch(sha)
    if not branch:
        return "error"

    if DRY_RUN:
        log(f"DRY_RUN=1 -> prepared branch '{branch}' locally; not pushing / not opening PR.",
            "WARNING")
        return "pr_opened"

    rc, _, err = run(["git", "push", "-f", push_target(), f"HEAD:{branch}"],
                     cwd=REPO_DIR, check=False)
    if rc != 0:
        log(f"push of revert branch failed: {err}", "ERROR")
        return "error"

    title = f"Rollback: revert bad deploy {sha[:10]}"
    body = (
        "Automated rollback proposal from the DevOps agent.\n\n"
        f"- Namespace: `{NAMESPACE}`\n"
        f"- Deployment: `{DEPLOYMENT}`\n"
        f"- Reverting commit: `{sha}`\n"
        "- Reason: pods failed to become Ready (bad image / crash loop).\n\n"
        "**Merging this PR rolls the cluster back via ArgoCD.** "
        "Review and merge to approve."
    )
    status, resp = gh_api("POST", f"/repos/{repo}/pulls",
                          {"title": title, "head": branch, "base": BRANCH, "body": body})
    if status not in (200, 201):
        log(f"Failed to open PR ({status}): {resp.get('error')}", "ERROR")
        return "error"

    pr_number = resp["number"]
    pr_url = resp["html_url"]
    log(f"Opened revert PR #{pr_number}: {pr_url}", "SUCCESS")

    if REVIEWERS:
        gh_api("POST", f"/repos/{repo}/pulls/{pr_number}/requested_reviewers",
               {"reviewers": REVIEWERS})
        log(f"Requested review from: {', '.join(REVIEWERS)}", "INFO")

    notify_slack(f":rewind: Rollback PR needs approval: <{pr_url}|#{pr_number}> "
                 f"({DEPLOYMENT} in {NAMESPACE})")

    log("Waiting for a human to approve + merge. Cluster is unchanged until then.", "WARNING")

    if WAIT_FOR_MERGE:
        return wait_for_merge(repo, pr_number, pr_url)
    return "pr_opened"


def wait_for_merge(repo, pr_number, pr_url):
    deadline = time.time() + MERGE_TIMEOUT
    while time.time() < deadline:
        status, resp = gh_api("GET", f"/repos/{repo}/pulls/{pr_number}")
        if status == 200:
            if resp.get("merged"):
                log(f"PR #{pr_number} merged. ArgoCD will now sync the rollback.", "SUCCESS")
                return "rolled_back"
            if resp.get("state") == "closed":
                log(f"PR #{pr_number} closed without merging. No rollback applied.", "WARNING")
                return "pr_opened"
        log(f"Awaiting approval on PR #{pr_number}... ({int(deadline - time.time())}s left)")
        time.sleep(POLL)
    log(f"Timed out waiting for approval on PR #{pr_number}: {pr_url}", "WARNING")
    return "pr_opened"


def rollback_direct(sha):
    """No approval gate: revert on main and push. Kept for non-gated setups."""
    run(["git", "checkout", BRANCH], cwd=REPO_DIR, check=False)
    run(["git", "reset", "--hard", f"origin/{BRANCH}"], cwd=REPO_DIR, check=False)
    run(["git", "config", "user.name", "rollback-agent"], cwd=REPO_DIR, check=False)
    run(["git", "config", "user.email", "rollback-agent@users.noreply.github.com"],
        cwd=REPO_DIR, check=False)
    rc, _, err = run(["git", "revert", "--no-edit", sha], cwd=REPO_DIR, check=False)
    if rc != 0:
        log(f"git revert failed: {err}", "ERROR")
        return "error"
    if DRY_RUN:
        log("DRY_RUN=1 -> revert committed locally but NOT pushed.", "WARNING")
        return "rolled_back"
    rc, _, err = run(["git", "push", push_target(), f"HEAD:{BRANCH}"], cwd=REPO_DIR, check=False)
    if rc != 0:
        log(f"git push failed: {err}", "ERROR")
        return "error"
    log("Revert pushed to main. ArgoCD will sync back to the previous image.", "SUCCESS")
    return "rolled_back"


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def watch_and_rollback():
    """
    Watch for TIMEOUT seconds, then act. Returns:
      "healthy"     deploy came up fine, nothing done
      "pr_opened"   failure detected, revert PR opened, awaiting human approval
      "rolled_back" revert merged/pushed (direct mode, or WAIT_FOR_MERGE saw the merge)
      "error"       something went wrong
    """
    log(f"Watching {DEPLOYMENT} in ns/{NAMESPACE} (selector {SELECTOR}) for up to {TIMEOUT}s...")
    deadline = time.time() + TIMEOUT
    while time.time() < deadline:
        state = evaluate_health()
        if state == "healthy":
            log("Deploy is healthy. No rollback needed.", "SUCCESS")
            return "healthy"
        if state == "failing":
            log("Detected failing pods (bad image / crash loop).", "WARNING")
            break
        log(f"Not ready yet... ({int(deadline - time.time())}s left)")
        time.sleep(POLL)
    else:
        log("Timed out waiting for pods to become Ready.", "WARNING")

    sha = bad_deploy_sha()
    if not sha:
        log("Could not resolve the deploy commit; aborting.", "ERROR")
        return "error"
    if not commit_touched_manifest(sha):
        log(f"Tip commit {sha[:10]} did not change k8s/; not reverting.", "WARNING")
        return "error"

    if ROLLBACK_MODE == "direct":
        return rollback_direct(sha)
    return rollback_via_pr(sha)


if __name__ == "__main__":
    result = watch_and_rollback()
    sys.exit(0 if result == "healthy" else 1)
