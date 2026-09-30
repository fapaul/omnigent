"""``omnigent agent add|list|remove`` against a mocked ``/v1/agents``."""

from __future__ import annotations

import gzip
import io
import tarfile
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from omnigent import cli as cli_mod


@pytest.fixture()
def server(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Route the CLI's API client to an in-memory ``/v1/agents``."""
    seen: list[httpx.Request] = []
    rows = [
        {"id": "ag_polly", "name": "polly", "version": 1, "installed": False},
        {"id": "ag_orion", "name": "orion", "version": 2, "installed": True, "harness": "codex"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"id": "ag_orion", "name": "orion", "version": 3})
        if request.method == "DELETE":
            return httpx.Response(200, json={"id": "ag_orion", "deleted": True})
        return httpx.Response(200, json={"data": rows, "has_more": False})

    monkeypatch.setattr(
        cli_mod,
        "_agent_api_client",
        lambda server: httpx.Client(transport=httpx.MockTransport(handler), base_url="http://t"),
    )
    return seen


def test_add_uploads_the_whole_directory(tmp_path: Path, server: list[httpx.Request]) -> None:
    (tmp_path / "config.yaml").write_text("spec_version: 1\nname: orion\n")
    (tmp_path / "agents" / "worker").mkdir(parents=True)
    (tmp_path / "agents" / "worker" / "config.yaml").write_text("name: worker\n")

    result = CliRunner().invoke(cli_mod.cli, ["agent", "add", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Installed orion (version 3" in result.output
    body = server[0].read()
    boundary = server[0].headers["content-type"].split("boundary=")[1].encode()
    start = body.index(b"\x1f\x8b")  # the gzip part of the multipart body
    tar_bytes = body[start : body.index(b"\r\n--" + boundary, start)]
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(tar_bytes)), mode="r:") as tf:
        names = {n.lstrip("./") for n in tf.getnames()}
    assert {"config.yaml", "agents/worker/config.yaml"} <= names


def test_list_shows_only_installed(server: list[httpx.Request]) -> None:
    result = CliRunner().invoke(cli_mod.cli, ["agent", "list"])
    assert result.exit_code == 0, result.output
    assert "orion" in result.output
    assert "polly" not in result.output


def test_remove_deletes_by_id(server: list[httpx.Request]) -> None:
    result = CliRunner().invoke(cli_mod.cli, ["agent", "remove", "orion"])
    assert result.exit_code == 0, result.output
    assert [(r.method, r.url.path) for r in server][-1] == ("DELETE", "/v1/agents/ag_orion")


def test_remove_unknown_name_fails(server: list[httpx.Request]) -> None:
    result = CliRunner().invoke(cli_mod.cli, ["agent", "remove", "polly"])
    assert result.exit_code != 0
    assert "No installed agent named 'polly'" in result.output
