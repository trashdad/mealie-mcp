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

from mealie_sous_chef import server  # noqa: E402
from mealie_sous_chef.mealie import MealieClient  # noqa: E402


@pytest.fixture
def fake() -> FakeMealie:
    return FakeMealie()


@pytest.fixture
async def client(fake: FakeMealie):
    c = MealieClient("http://mealie.test", "test-token", transport=fake.transport())
    yield c
    await c.aclose()


@pytest.fixture
async def tools(client: MealieClient, monkeypatch):
    """Call MCP tools the way Claude does (argument validation, error wrapping
    included), against the fake Mealie. Returns (call, call_error)."""
    monkeypatch.setattr(server, "_mealie", client)

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
