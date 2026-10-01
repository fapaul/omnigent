"""Verify recovery after a cached Claude SDK CLI child exits between turns."""

from __future__ import annotations

import contextlib
import os
import signal
import time
import uuid

import httpx
import psutil
import pytest
import yaml
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _create_bundled_session, _ensure_runner_online, _server_state

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'
_ERROR_PILL = '[data-testid="error-pill"]'
_ERROR_CONTENT = '[data-testid="error-message-content"]'

_TERMINATED_TEXT = "Cannot write to terminated process"

_CONTEXT_WINDOW = 200_000


def _claude_sdk_agent_yaml(name: str, mock_llm_server_url: str) -> str:
    """Render the claude-sdk agent spec that targets the mock endpoint."""
    config = {
        "name": name,
        "prompt": "You are a terse assistant. Answer in as few words as possible.",
        "executor": {
            "harness": "claude-sdk",
            "model": "claude-sonnet-4-20250514",
            "context_window": _CONTEXT_WINDOW,
            "auth": {
                "type": "api_key",
                "api_key": "mock-key",
                "base_url": mock_llm_server_url,
            },
        },
    }
    return yaml.safe_dump(config, sort_keys=False)


def _claude_cli_pids() -> set[int]:
    """Return live SDK-launched Claude CLI process IDs."""
    pids: set[int] = set()
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (proc.info["name"] or "").lower()
            cmd = " ".join(proc.info["cmdline"] or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if "stream-json" not in cmd:
            continue
        if name == "claude" or "/claude" in cmd.lower():
            pids.add(proc.info["pid"])
    return pids


def _send(page: Page, text: str) -> None:
    """Type *text* into the composer and click Send."""
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible()
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


@pytest.mark.timeout(600)
def test_next_turn_recovers_when_claude_cli_was_terminated(
    page: Page,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Recover the next turn after the cached Claude CLI exits."""
    from tests.e2e_ui.conftest import configure_mock_llm

    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    try:
        runner_id = str(_server_state["runner_id"])
        session_id = _create_bundled_session(
            live_server,
            runner_id,
            _claude_sdk_agent_yaml(f"sdk-term-{uuid.uuid4().hex[:8]}", mock_llm_server_url),
        )
        try:
            uid = uuid.uuid4().hex[:6]
            token1 = f"sdkterm-one-{uid}"
            token2 = f"sdkterm-two-{uid}"

            configure_mock_llm(
                mock_llm_server_url,
                [{"text": "ack one"}] * 6,
                key=f"sdkterm-turn1-{uid}",
                match=token1,
            )
            configure_mock_llm(
                mock_llm_server_url,
                [{"text": "ack two"}] * 6,
                key=f"sdkterm-turn2-{uid}",
                match=token2,
            )

            page.goto(f"{live_server}/c/{session_id}")

            baseline_pids = _claude_cli_pids()
            _send(page, f"Say ack. {token1}")
            expect(page.locator(_ASSISTANT).first).to_be_visible(timeout=180_000)
            expect(page.locator(_WORKING)).to_have_count(0, timeout=180_000)
            assistant_after_turn1 = page.locator(_ASSISTANT).count()

            new_pids: set[int] = set()
            deadline = time.time() + 30
            while time.time() < deadline:
                new_pids = _claude_cli_pids() - baseline_pids
                if new_pids:
                    break
                time.sleep(0.5)
            assert new_pids, (
                "expected a claude-sdk CLI child process to be running after turn 1; "
                f"baseline={baseline_pids}, now={_claude_cli_pids()}"
            )
            for pid in new_pids:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGTERM)
            for pid in new_pids:
                try:
                    proc = psutil.Process(pid)
                    exit_code = proc.wait(timeout=20)
                    print(f"claude CLI pid {pid} exited with {exit_code}")
                except psutil.NoSuchProcess:
                    # Already exited and reaped before wait() could observe it.
                    pass
                except psutil.TimeoutExpired:
                    proc.kill()
            # Let the runner reap the child before the next transport write.
            time.sleep(2.0)

            _send(page, f"Continue. {token2}")

            terminal_deadline = time.time() + 240
            error_present = False
            while time.time() < terminal_deadline:
                error_present = page.locator(_ERROR_PILL).count() > 0
                reply_present = page.locator(_ASSISTANT).count() > assistant_after_turn1
                if error_present or reply_present:
                    break
                page.wait_for_timeout(500)

            if error_present:
                try:
                    page.locator(_ERROR_PILL).first.click()
                    expect(page.locator(_ERROR_CONTENT)).to_be_visible(timeout=5_000)
                    page.wait_for_timeout(1_500)
                except Exception:  # best-effort surfacing for the recording only
                    pass

            expect(page.get_by_text("ack two")).to_be_visible(timeout=60_000)
            expect(page.locator(_ERROR_PILL)).to_have_count(0)
            expect(page.get_by_text(_TERMINATED_TEXT)).to_have_count(0)
        finally:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
    finally:
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except Exception:  # best-effort teardown
                respawned.kill()
                respawned.wait(timeout=5)
