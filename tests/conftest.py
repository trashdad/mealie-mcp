from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

# server.py builds Settings() at import time; give it a harmless environment.
_DATA_DIR = tempfile.mkdtemp(prefix="sous-chef-tests-")
os.environ.update(
    {
        "MEALIE_URL": "http://mealie.test",
        "MEALIE_API_TOKEN": "test-token",
        "PUBLIC_URL": "https://sous-chef.test",
        "MCP_LOGIN_PASSWORD": "correct-horse-battery-staple",
        "MCP_DATA_DIR": _DATA_DIR,
    }
)

from fake_mealie import FakeMealie  # noqa: E402

import httpx  # noqa: E402

from mealie_sous_chef import server  # noqa: E402
from mealie_sous_chef.mealie import MealieClient  # noqa: E402
from mealie_sous_chef.nutrition_sources import NutritionSources  # noqa: E402


@pytest.fixture
def fake() -> FakeMealie:
    return FakeMealie()


@pytest.fixture
async def client(fake: FakeMealie):
    c = MealieClient("http://mealie.test", "test-token", transport=fake.transport())
    yield c
    await c.aclose()


class FakeUSDA:
    """USDA food-detail records by FDC id (household measures only)."""

    def __init__(self):
        self.foods: dict[str, dict] = {}
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        fdc = request.url.path.rsplit("/", 1)[-1]
        if request.url.host == "api.nal.usda.gov" and fdc in self.foods:
            return httpx.Response(200, json=self.foods[fdc])
        return httpx.Response(404)


@pytest.fixture
def usda() -> FakeUSDA:
    return FakeUSDA()


@pytest.fixture
async def tools(client: MealieClient, usda: FakeUSDA, monkeypatch, tmp_path):
    """Call MCP tools the way Claude does (argument validation, error wrapping
    included), against the fake Mealie. Returns (call, call_error)."""
    monkeypatch.setattr(server, "_mealie", client)
    sources = NutritionSources(str(tmp_path), "key", transport=httpx.MockTransport(usda.handler))
    monkeypatch.setattr(server, "_nutrition", sources)

    async def call(tool: str, /, **arguments):
        result = await server.mcp.call_tool(tool, arguments)
        if result.structured_content is not None:
            content = result.structured_content
            return content.get("result", content) if set(content) == {"result"} else content
        return json.loads(result.content[0].text)

    async def call_error(tool: str, /, **arguments) -> str:
        from mcp.server.mcpserver.exceptions import ToolError

        with pytest.raises(ToolError) as exc_info:
            await server.mcp.call_tool(tool, arguments)
        return str(exc_info.value)

    return call, call_error
