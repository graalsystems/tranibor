"""A client that reads a project or a policy, edits it and sends it back resurrects no former field
(ADR 0181 C2, the window closed on 2026-11-23; S83-04e3 noted the round trip).

The `delivery` section is the one place a project's repositories and a policy's diff bounds, merge rule
and trains live: what the client removes from it stays removed, nothing is derived back from a field it
did not send, and a request that writes a former field is refused (422), naming the section.
"""

from __future__ import annotations

from typing import Any

import yaml
from httpx import AsyncClient

FORMER_PROJECT_FIELDS = {"repo", "repositories", "gitops"}


async def test_a_project_read_edited_and_sent_back_keeps_its_section_only(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    pid = project["id"]
    read = (await client.get(f"/api/v1/projects/{pid}")).json()
    config = read["config"]
    assert not FORMER_PROJECT_FIELDS & set(config), "a project is answered with its section only"
    assert config["delivery"]["repo"]["url"].endswith("/billing-api.git")

    # The client edits another field and sends the whole configuration back.
    edited = {**config, "notify": {"slack_channel": "#billing"}}
    saved = await client.patch(f"/api/v1/projects/{pid}", json={"config": edited})
    assert saved.status_code == 200, saved.text
    assert not FORMER_PROJECT_FIELDS & set(saved.json()["config"])
    assert saved.json()["config"]["delivery"] == config["delivery"]

    # It removes the repository: it stays removed, nothing brings it back.
    without = {k: v for k, v in edited.items() if k != "delivery"}
    saved = await client.patch(f"/api/v1/projects/{pid}", json={"config": without})
    assert saved.status_code == 200, saved.text
    again = (await client.get(f"/api/v1/projects/{pid}")).json()["config"]
    assert "delivery" not in again and not FORMER_PROJECT_FIELDS & set(again)


async def test_a_project_written_with_a_former_field_is_refused(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    pid = project["id"]
    config = (await client.get(f"/api/v1/projects/{pid}")).json()["config"]
    former = {**{k: v for k, v in config.items() if k != "delivery"}, "repo": config["delivery"]["repo"]}
    refused = await client.patch(f"/api/v1/projects/{pid}", json={"config": former})
    assert refused.status_code == 422, refused.text
    assert "moved to the `delivery` section" in refused.text
    created = await client.post(
        "/api/v1/orgs/northwind/projects",
        json={
            "slug": "other",
            "name": "Other",
            "config": {"slug": "other", "org": "northwind", "gitops": {}},
        },
    )
    assert created.status_code == 422, created.text


async def test_a_policy_read_edited_and_sent_back_keeps_its_section_only(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    pid = project["id"]
    read = (await client.get(f"/api/v1/projects/{pid}/policy")).json()
    document = yaml.safe_load(read["yaml"])
    assert "release_train" not in document and "max_diff_files" not in document.get("scope", {})
    assert "release_train" not in read["json"] and "max_diff_files" not in read["json"]["scope"]

    # The client tightens the diff bounds and drops the trains, then sends the text back.
    document["delivery"] = {"scope": {"max_diff_files": 7, "max_diff_lines": 70}}
    saved = await client.put(f"/api/v1/projects/{pid}/policy", json={"yaml": yaml.safe_dump(document)})
    assert saved.status_code == 200, saved.text
    stored = saved.json()["json"]
    assert stored["delivery"]["scope"] == {"max_diff_files": 7, "max_diff_lines": 70}
    assert stored["delivery"]["release_train"] == {}, "a removed train stays removed"
    assert "release_train" not in stored and "max_diff_files" not in stored["scope"]
    again = (await client.get(f"/api/v1/projects/{pid}/policy")).json()
    assert yaml.safe_load(again["yaml"])["delivery"]["scope"]["max_diff_files"] == 7

    former = {**document, "release_train": {"prod": {"mode": "train"}}}
    refused = await client.put(f"/api/v1/projects/{pid}/policy", json={"yaml": yaml.safe_dump(former)})
    assert refused.status_code == 422, refused.text
    assert "moved to the `delivery` section" in refused.text
