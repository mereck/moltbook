"""
Sandboxed agent that reads moltbook.com and discusses it using MonadGPT,
a 17th-century language model. Runs as non-root inside a locked-down
Docker container with no host access.
"""

import json
import os
import sys

import requests
from bs4 import BeautifulSoup

TARGET_URL = os.environ.get("TARGET_URL", "https://www.moltbook.com")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
MODEL = "brxce/monadgpt"


def fetch_page(url: str) -> str:
    """Fetch a page and return its text content."""
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text(separator="\n", strip=True)


def ask_monadgpt(prompt: str) -> str:
    """Send a prompt to MonadGPT via Ollama and return the response."""
    resp = requests.post(
        f"{OLLAMA_URL}/api/generate",
        json={
            "model": MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.8,
                "num_predict": 512,
            },
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json().get("response", "")


def main():
    print(f"[agent] starting")
    print(f"[agent] target : {TARGET_URL}")
    print(f"[agent] llm    : {MODEL} @ {OLLAMA_URL}")
    print()

    # Step 1: Fetch moltbook.com
    print(f"[agent] fetching {TARGET_URL}...")
    try:
        page_text = fetch_page(TARGET_URL)
        print(f"[agent] got {len(page_text)} chars")
    except Exception as e:
        print(f"[agent] ERROR fetching page: {e}", file=sys.stderr)
        sys.exit(1)

    # Step 2: Ask MonadGPT to comment on it
    snippet = page_text[:1500]
    prompt = (
        f"You are MonadGPT, a learned scholar from the 17th century. "
        f"A traveller hath brought you a curious document from the future. "
        f"Read it and give your observations.\n\n"
        f"--- DOCUMENT ---\n{snippet}\n--- END ---\n\n"
        f"What say you, learned scholar?"
    )

    print(f"[agent] asking MonadGPT for commentary...")
    try:
        reply = ask_monadgpt(prompt)
        print()
        print("=" * 60)
        print("MonadGPT speaks:")
        print("=" * 60)
        print(reply)
        print("=" * 60)
    except Exception as e:
        print(f"[agent] ERROR from LLM: {e}", file=sys.stderr)
        sys.exit(1)

    print()
    print("[agent] done")


if __name__ == "__main__":
    main()
