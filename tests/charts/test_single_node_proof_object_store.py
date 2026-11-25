"""S80-34, #2187: the single-node proof gives the installation the object store its seed writes to.

The single-node profile embeds no object store (`global.objectStore` is the site's, ADR 0184). The
proof's seed (`tranibor_api.local_demo`, through `tools/single-node/work_item.py`) hands the
procurement work item its files, so the installation the proof makes needs one: without it the API
kept the chart's default, `s3://choregos` on AWS, and the seed died on `NoSuchBucket` before any
backup was played. The proof now points `global.objectStore` at its off-node S3 store, creates the
bucket there before the install, and the backup copies it.

The chart is rendered here with the very `--set` arguments `proof.sh` passes to `helm install`, read
from the script, so a proof that stops naming its object store turns this red.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
from typing import Any

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
CHART = ROOT / "charts" / "tranibor"
PROFILE = CHART / "values" / "single-node.yaml"
PROOF = ROOT / "tools" / "single-node" / "proof.sh"
#: What the k3d branch of `s3_up` computes at run time: the SeaweedFS container's address.
ENDPOINT = "http://172.18.0.3:8333"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm absent")


def _function(name: str) -> str:
    text = PROOF.read_text(encoding="utf-8")
    found = re.search(rf"^{name}\(\) \{{\n(.*?)^\}}\n", text, re.S | re.M)
    assert found, f"proof.sh has no function {name}"
    return found.group(1)


def _k3d_variables() -> dict[str, str]:
    """The literal assignments of `s3_up`'s k3d branch, and the variables `proof.sh` sets on top."""
    k3d = _function("s3_up").split("\n  else\n", 1)[0]
    variables = dict(re.findall(r"^\s+([A-Z0-9_]+)=([A-Za-z0-9._-]+)\s*$", k3d, re.M))
    head = PROOF.read_text(encoding="utf-8")
    for name in ("NAME", "ORG", "ADMIN"):
        found = re.search(rf"^{name}=(?:\$\{{[A-Z_]+:-)?([^}}\s]+)\}}?$", head, re.M)
        assert found, name
        variables[name] = found.group(1)
    return variables | {"S3_ENDPOINT": ENDPOINT}


def _created_buckets() -> set[str]:
    """The buckets the k3d branch of `s3_up` creates in the off-node store, by variable name."""
    k3d = _function("s3_up").split("\n  else\n", 1)[0]
    created = set(re.findall(r"s3\.bucket\.create -name \$([A-Za-z_]+)", k3d))
    loops = re.findall(r"for (\w+) in ([^;]+); do", k3d)
    names: set[str] = set()
    for variable in created:
        listed = [items for loop, items in loops if loop == variable]
        if listed:
            names |= set(re.findall(r"\$([A-Z0-9_]+)", listed[0]))
        else:
            names.add(variable)
    variables = _k3d_variables()
    return {variables[name] for name in names if name in variables}


def _install_sets() -> list[str]:
    """The `--set` arguments of `install`'s `helm install`, the shell variables replaced."""
    variables = _k3d_variables()
    sets = re.findall(r"--set (\S+)", _function("install"))
    rendered = []
    for raw in sets:
        if "PROOF_IMAGE_TAG" in raw:
            continue
        value = raw.replace('"', "")
        value = re.sub(r"\$([A-Z0-9_]+)", lambda m: variables[m.group(1)], value)
        rendered.append(value)
    return rendered


def _documents() -> list[dict[str, Any]]:
    arguments = [part for value in _install_sets() for part in ("--set", value)]
    command = ["helm", "template", "choregos", str(CHART), "-n", "choregos", "-f", str(PROFILE), *arguments]
    rendered = subprocess.run(command, capture_output=True, text=True, check=True).stdout
    return [d for d in yaml.safe_load_all(rendered) if d]


def _env(container: dict[str, Any]) -> dict[str, Any]:
    return {e["name"]: e.get("value", e.get("valueFrom")) for e in container.get("env", [])}


def test_the_proof_s_api_writes_its_files_to_a_bucket_the_proof_created() -> None:
    documents = _documents()
    api = next(d for d in documents if (d["kind"], d["metadata"]["name"]) == ("Deployment", "tranibor-api"))
    env = _env(api["spec"]["template"]["spec"]["containers"][0])
    # Not AWS: an empty endpoint is AWS's, where the bucket `choregos` is nobody's the proof knows.
    assert env["CHOREGOS_OBJECT_STORE_ENDPOINT"] == ENDPOINT, env.get("CHOREGOS_OBJECT_STORE_ENDPOINT")
    bucket = env["CHOREGOS_OBJECT_STORE_URL"].removeprefix("s3://").split("/", 1)[0]
    created = _created_buckets()
    assert bucket in created, f"the API writes to `{bucket}`, the proof creates {created}"
    assert "CHOREGOS_OBJECT_STORE_ACCESS_KEY" in env, "the store's keys come from a Secret"
    secret = env["CHOREGOS_OBJECT_STORE_ACCESS_KEY"]["secretKeyRef"]["name"]
    assert f"create secret generic {secret}" in _function("install"), f"proof.sh never creates {secret}"


def test_the_proof_s_backup_copies_the_object_store_and_shows_it() -> None:
    documents = _documents()
    backup = next(
        d for d in documents if (d["kind"], d["metadata"]["name"]) == ("CronJob", "choregos-backup")
    )
    steps = [c["name"] for c in backup["spec"]["jobTemplate"]["spec"]["template"]["spec"]["initContainers"]]
    assert "objects" in steps, steps
    shown = re.search(r"for c in ([a-z ]+); do k logs job/proof-backup", PROOF.read_text(encoding="utf-8"))
    assert shown and "objects" in shown.group(1).split(), "the proof's log leaves the objects step out"
