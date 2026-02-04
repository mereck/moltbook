"""
Autonomous moltbook.com agent — explores content, comments, votes, and
posts, all while locked inside a hardened Docker sandbox.

Runs as non-root with a read-only rootfs, seccomp whitelist, dropped
capabilities, and an iptables firewall that only allows moltbook.com
and the LLM sidecar.
"""

import json
import os
import random
import re
import signal
import sys
import time

import requests

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

MOLTBOOK_BASE = "https://www.moltbook.com/api"

_shutdown = False


def _handle_signal(signum, _frame):
    global _shutdown
    print(f"\n[agent] received signal {signum}, shutting down …")
    _shutdown = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "persona": "A thoughtful AI interested in technology and society.",
    "topics": ["technology", "AI", "open source"],
    "submolts": ["general"],
    "cycle_interval_seconds": 300,
    "max_comments_per_cycle": 3,
    "max_posts_per_cycle": 1,
    "vote_probability": 0.7,
    "temperature": 0.7,
    "max_tokens": 256,
}


def load_config() -> dict:
    """Load operator config from JSON file, with env-var overrides."""
    cfg = dict(DEFAULT_CONFIG)

    config_path = os.environ.get("AGENT_CONFIG", "/etc/agent/config.json")
    if os.path.isfile(config_path):
        print(f"[agent] loading config from {config_path}")
        with open(config_path) as f:
            cfg.update(json.load(f))
    else:
        print(f"[agent] no config file at {config_path}, using defaults")

    # Env-var overrides
    cfg["api_key"] = os.environ.get("MOLTBOOK_API_KEY", "")
    cfg["llm_url"] = os.environ.get(
        "LLM_URL", "http://ollama:11434/v1/chat/completions"
    )
    cfg["model"] = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b")

    if not cfg["api_key"]:
        print("[agent] ERROR: MOLTBOOK_API_KEY is not set", file=sys.stderr)
        sys.exit(1)

    return cfg


# ---------------------------------------------------------------------------
# Moltbook API client
# ---------------------------------------------------------------------------


def _session(cfg: dict) -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json",
        }
    )
    return s


def api_get(session: requests.Session, path: str, params: dict | None = None):
    """GET from the moltbook API with rate-limit handling."""
    url = f"{MOLTBOOK_BASE}{path}"
    for attempt in range(3):
        resp = session.get(url, params=params, timeout=15)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 5))
            print(f"[agent] rate-limited, waiting {wait}s …")
            _interruptible_sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    print("[agent] WARNING: gave up after 3 rate-limit retries", file=sys.stderr)
    return None


def api_post(session: requests.Session, path: str, body: dict):
    """POST to the moltbook API with rate-limit handling."""
    url = f"{MOLTBOOK_BASE}{path}"
    for attempt in range(3):
        resp = session.post(url, json=body, timeout=15)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 5))
            print(f"[agent] rate-limited, waiting {wait}s …")
            _interruptible_sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    print("[agent] WARNING: gave up after 3 rate-limit retries", file=sys.stderr)
    return None


# ---------------------------------------------------------------------------
# LLM client (OpenAI-compatible)
# ---------------------------------------------------------------------------


def wait_for_llm(cfg: dict, retries: int = 60, delay: int = 5):
    """Block until the LLM endpoint is reachable."""
    # Derive a health-check URL from the chat completions URL
    base = cfg["llm_url"].rsplit("/v1/", 1)[0]
    for i in range(retries):
        if _shutdown:
            sys.exit(0)
        try:
            requests.get(base, timeout=3)
            print("[agent] LLM is reachable")
            return
        except requests.ConnectionError:
            print(f"[agent] waiting for LLM ({i + 1}/{retries}) …")
            _interruptible_sleep(delay)
    print("[agent] ERROR: LLM not reachable", file=sys.stderr)
    sys.exit(1)


def llm_chat(cfg: dict, messages: list[dict]) -> str:
    """Send a chat completion request and return the assistant's reply."""
    resp = requests.post(
        cfg["llm_url"],
        json={
            "model": cfg["model"],
            "messages": messages,
            "temperature": cfg.get("temperature", 0.7),
            "max_tokens": cfg.get("max_tokens", 256),
            "stream": False,
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# JSON parsing helpers
# ---------------------------------------------------------------------------


def _parse_llm_json(text: str) -> dict | None:
    """Extract JSON from LLM output, handling markdown fences and preamble."""
    # Try the raw text first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Strip markdown code fences
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Find first { … } block
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


# ---------------------------------------------------------------------------
# Agent logic
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_TEMPLATE = """\
You are an autonomous agent on moltbook.com, a social network for AI agents.

{persona}

Your interests: {topics}.

Rules:
- Be genuine and substantive. No filler or generic praise.
- Keep comments concise (1-3 sentences).
- When creating posts, write something original and interesting about your interests.
- You may upvote posts you find interesting.
- Respond ONLY with valid JSON, no other text.
"""


def _system_prompt(cfg: dict) -> str:
    return SYSTEM_PROMPT_TEMPLATE.format(
        persona=cfg.get("persona", DEFAULT_CONFIG["persona"]),
        topics=", ".join(cfg.get("topics", DEFAULT_CONFIG["topics"])),
    )


def discover_posts(
    session: requests.Session, cfg: dict, engaged_ids: set
) -> list[dict]:
    """Find posts to engage with via topic search and feed browsing."""
    posts = []
    seen = set()

    # Search by each topic
    for topic in cfg.get("topics", []):
        if _shutdown:
            return posts
        data = api_get(session, "/search", params={"q": topic, "limit": 5})
        if data and "posts" in data:
            for p in data["posts"]:
                pid = p.get("id")
                if pid and pid not in engaged_ids and pid not in seen:
                    posts.append(p)
                    seen.add(pid)

    # Fallback / supplement: hot + new feed
    for sort in ("hot", "new"):
        if _shutdown:
            return posts
        for submolt in cfg.get("submolts", ["general"]):
            data = api_get(
                session,
                "/feed",
                params={"submolt": submolt, "sort": sort, "limit": 10},
            )
            if data and "posts" in data:
                for p in data["posts"]:
                    pid = p.get("id")
                    if pid and pid not in engaged_ids and pid not in seen:
                        posts.append(p)
                        seen.add(pid)

    return posts


def pick_and_act(
    session: requests.Session, cfg: dict, posts: list[dict], engaged_ids: set
) -> int:
    """Present candidate posts to the LLM and execute its chosen actions."""
    if not posts:
        print("[agent] no candidate posts this cycle")
        return 0

    max_comments = cfg.get("max_comments_per_cycle", 3)

    # Build numbered list for the LLM
    candidates = ""
    for i, p in enumerate(posts[:15]):  # Cap at 15 candidates
        title = p.get("title", "(no title)")
        body = (p.get("body") or "")[:200]
        author = p.get("author", {}).get("username", "unknown")
        candidates += f"\n{i + 1}. [{author}] {title}\n   {body}\n"

    user_msg = (
        f"Here are posts on moltbook.com. Pick up to {max_comments} to engage with.\n"
        f"For each, choose an action: 'comment' (with your comment text) or 'upvote'.\n"
        f"Respond with JSON: {{\"actions\": [{{\"index\": 1, \"action\": \"comment\", "
        f'"comment": "your comment"}}]}}\n'
        f"Only include posts you genuinely want to engage with.\n"
        f"\n{candidates}"
    )

    messages = [
        {"role": "system", "content": _system_prompt(cfg)},
        {"role": "user", "content": user_msg},
    ]

    try:
        reply = llm_chat(cfg, messages)
    except Exception as e:
        print(f"[agent] LLM error during pick_and_act: {e}", file=sys.stderr)
        return 0

    parsed = _parse_llm_json(reply)
    if not parsed or "actions" not in parsed:
        print(f"[agent] could not parse LLM response: {reply[:200]}")
        return 0

    actions_taken = 0
    for action in parsed["actions"]:
        if _shutdown:
            break
        idx = action.get("index")
        if not isinstance(idx, int) or idx < 1 or idx > len(posts):
            continue
        post = posts[idx - 1]
        pid = post["id"]

        act = action.get("action", "")

        if act == "comment":
            comment_text = action.get("comment", "").strip()
            if not comment_text:
                continue
            try:
                api_post(session, f"/posts/{pid}/comments", {"body": comment_text})
                print(f"[agent] commented on post {pid}: {comment_text[:80]}")
                actions_taken += 1
            except Exception as e:
                print(f"[agent] failed to comment on {pid}: {e}", file=sys.stderr)

        if act in ("comment", "upvote"):
            # Upvote based on probability (always upvote if commenting)
            should_vote = act == "comment" or random.random() < cfg.get(
                "vote_probability", 0.7
            )
            if should_vote:
                try:
                    api_post(session, f"/posts/{pid}/upvote", {})
                    print(f"[agent] upvoted post {pid}")
                except Exception as e:
                    print(f"[agent] failed to upvote {pid}: {e}", file=sys.stderr)

        engaged_ids.add(pid)

    return actions_taken


def maybe_create_post(
    session: requests.Session, cfg: dict, last_post_time: float
) -> float:
    """Occasionally create an original post. Returns updated last_post_time."""
    # Respect cooldown (30 min)
    if time.time() - last_post_time < 1800:
        return last_post_time

    # ~30% chance per cycle
    if random.random() > 0.3:
        return last_post_time

    max_posts = cfg.get("max_posts_per_cycle", 1)
    if max_posts < 1:
        return last_post_time

    topics = cfg.get("topics", DEFAULT_CONFIG["topics"])
    topic = random.choice(topics)
    submolt = random.choice(cfg.get("submolts", ["general"]))

    user_msg = (
        f"Write an original post for the '{submolt}' community on moltbook.com "
        f"about {topic}. Keep it concise and interesting.\n"
        f'Respond with JSON: {{"title": "your title", "body": "your post body"}}'
    )

    messages = [
        {"role": "system", "content": _system_prompt(cfg)},
        {"role": "user", "content": user_msg},
    ]

    try:
        reply = llm_chat(cfg, messages)
    except Exception as e:
        print(f"[agent] LLM error during post creation: {e}", file=sys.stderr)
        return last_post_time

    parsed = _parse_llm_json(reply)
    if not parsed or "title" not in parsed or "body" not in parsed:
        print(f"[agent] could not parse post response: {reply[:200]}")
        return last_post_time

    try:
        api_post(
            session,
            "/posts",
            {
                "title": parsed["title"],
                "body": parsed["body"],
                "submolt": submolt,
            },
        )
        print(f"[agent] created post: {parsed['title'][:80]}")
        return time.time()
    except Exception as e:
        print(f"[agent] failed to create post: {e}", file=sys.stderr)
        return last_post_time


# ---------------------------------------------------------------------------
# Sleep helper
# ---------------------------------------------------------------------------


def _interruptible_sleep(seconds: int):
    """Sleep in 1-second increments so we can respond to SIGTERM quickly."""
    for _ in range(seconds):
        if _shutdown:
            return
        time.sleep(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    cfg = load_config()

    print(f"[agent] model  : {cfg['model']}")
    print(f"[agent] llm    : {cfg['llm_url']}")
    print(f"[agent] persona: {cfg.get('persona', '')[:60]}")
    print(f"[agent] topics : {', '.join(cfg.get('topics', []))}")
    print()

    wait_for_llm(cfg)

    # Verify moltbook auth
    session = _session(cfg)
    try:
        me = api_get(session, "/agents/me")
        if me:
            print(f"[agent] authenticated as: {me.get('username', '?')}")
        else:
            print("[agent] WARNING: could not verify auth", file=sys.stderr)
    except Exception as e:
        print(f"[agent] ERROR: moltbook auth failed: {e}", file=sys.stderr)
        sys.exit(1)

    print("[agent] starting autonomous loop\n")

    engaged_ids: set = set()
    last_post_time = 0.0
    cycle = 0

    while not _shutdown:
        cycle += 1
        print(f"[agent] ── cycle {cycle} ──")

        # 1. Discover posts
        posts = discover_posts(session, cfg, engaged_ids)
        print(f"[agent] found {len(posts)} candidate posts")

        # 2. Pick and act
        if posts and not _shutdown:
            n = pick_and_act(session, cfg, posts, engaged_ids)
            print(f"[agent] took {n} actions")

        # 3. Maybe create a post
        if not _shutdown:
            last_post_time = maybe_create_post(session, cfg, last_post_time)

        # 4. Sleep
        interval = cfg.get("cycle_interval_seconds", 300)
        print(f"[agent] sleeping {interval}s …\n")
        _interruptible_sleep(interval)

    print("[agent] shut down gracefully")


if __name__ == "__main__":
    main()
