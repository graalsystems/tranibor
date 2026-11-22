"""The split of the trade into two plugins (#2134; the agents repository's #27).

`procurement` is what a deployment that runs no plugin server (the local `docker compose` install) can
install: the installer refuses a plugin that brings a server there. The server, with everything that needs
it, is `procurement-operations`. The two archives are the ones `plugins/offline/` locks.
"""

from __future__ import annotations

from tranibor_core.plugins import load_directory

from .content import OPERATIONS_ROOT, ROOT


def test_the_locked_procurement_archive_declares_no_server() -> None:
    plugin = load_directory(ROOT)
    assert plugin.servers == {}
    assert "servers" not in (plugin.manifest.get("contents") or {})
    assert not (ROOT / "servers").exists()
    assert plugin.manifest["requires"]["connectors"] == {}, "no connector a server would satisfy"


def test_the_server_is_in_the_operations_archive_which_needs_no_other_plugin() -> None:
    plugin = load_directory(OPERATIONS_ROOT)
    assert set(plugin.servers) == {"purchasing"}
    assert plugin.manifest["requires"]["plugins"] == []
