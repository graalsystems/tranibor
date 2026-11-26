"""What the adapters show and post under the new name (ADR 0226, R9c), the former still recognised.

The chat buttons' `action_id`, the MCP client's `clientInfo.name`, the fake tracker's state labels and the
extension loader's entry-point groups: each has a case for the new name and, where something is read, one
for the former.
"""

from __future__ import annotations

import json
import pathlib
import sys

import httpx
import pytest
import tranibor_adapters
from tranibor_adapters.fakes.mcp import FakeMcpServer
from tranibor_adapters.fakes.tracker import FakeTracker
from tranibor_adapters.mcp import McpClient
from tranibor_adapters.notify.slack import SlackNotifier
from tranibor_contracts import wire
from tranibor_core.domain import Message, MessageAction, TrackerStateMapping


async def test_a_slack_button_carries_the_new_action_prefix() -> None:
    sent: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    notifier = SlackNotifier(
        bot_token="xoxb-test", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    message = Message(
        title="t", url="https://tranibor.test/p", actions=[MessageAction(id="approve", label="Go")]
    )
    await notifier.send("#c", message)
    (actions,) = [block for block in sent[0]["blocks"] if block["type"] == "actions"]  # type: ignore[attr-defined]
    ids = [element["action_id"] for element in actions["elements"]]
    assert ids == ["tranibor:approve", "tranibor:open"]
    # A button posted before the upgrade names its action under the former prefix: still the platform's.
    assert [wire.chat_action_name(i) for i in [*ids, "choregos:approve"]] == ["approve", "open", "approve"]


async def test_the_mcp_client_announces_itself_under_the_new_name() -> None:
    server = FakeMcpServer(tools=[{"name": "t", "inputSchema": {"type": "object"}}])
    await McpClient("https://supplier.test/mcp", transport=server.transport()).list_tools()
    (initialize,) = [body for method, _, body in server.received if method == "initialize"]
    assert initialize["params"]["clientInfo"]["name"] == "tranibor"


@pytest.mark.parametrize("held", ["tranibor:inbox", "choregos:inbox"], ids=["new", "former"])
async def test_the_fake_tracker_keeps_one_state_label_under_either_prefix(held: str) -> None:
    tracker = FakeTracker()
    key = tracker.seed("t", "b", labels=[held, "bug"])
    await tracker.set_state(key, TrackerStateMapping(label="tranibor:implement"))
    assert tracker.items[key].labels == ["bug", "tranibor:implement"]


def _distribution(root: pathlib.Path, name: str, groups: list[str]) -> None:
    (root / f"{name}.py").write_text(
        "import sys\nLOADED = sys.modules.setdefault('r9c_loaded', type(sys)('r9c_loaded'))\n"
        f"def plug_in():\n    LOADED.__dict__.setdefault('names', []).append({name!r})\n",
        encoding="utf-8",
    )
    info = root / f"{name}-1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n", encoding="utf-8")
    sections = "".join(f"[{group}]\n{name} = {name}:plug_in\n" for group in groups)
    (info / "entry_points.txt").write_text(sections, encoding="utf-8")


def test_extensions_load_from_both_groups_once_each(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plugin installed from outside the repository may still register under `choregos.plugins`; one that
    registers under both groups is loaded once."""
    assert tranibor_adapters.EXTENSION_GROUP == "tranibor.plugins"
    _distribution(tmp_path, "r9c_ext_new", ["tranibor.plugins"])
    _distribution(tmp_path, "r9c_ext_former", ["choregos.plugins"])
    _distribution(tmp_path, "r9c_ext_both", ["tranibor.plugins", "choregos.plugins"])
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "r9c_loaded", raising=False)
    from tranibor_adapters import registry

    # The installed extensions register in module-level tables: put them back as they were.
    monkeypatch.setattr(registry, "_REGISTRY", dict(registry._REGISTRY))
    monkeypatch.setattr(registry, "_SPECS", dict(registry._SPECS))
    loaded = tranibor_adapters.load_extensions()
    ours = [name for name in loaded if name.startswith("r9c_ext_")]
    assert sorted(ours) == ["r9c_ext_both", "r9c_ext_former", "r9c_ext_new"]
    assert sorted(sys.modules["r9c_loaded"].names) == ["r9c_ext_both", "r9c_ext_former", "r9c_ext_new"]
