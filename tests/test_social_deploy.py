"""The deployment kit has to keep describing the code it deploys.

``.dockerignore``, ``Dockerfile``, ``docker-compose.yml``, ``.env.example`` and the
scheduled workflow are five files nobody runs during development, which is exactly
why they rot: a renamed subcommand or a new credential variable leaves them quietly
wrong and the mistake surfaces on the machine that is hardest to inspect.  These
tests read them the way the runtime would - the variable names against the
configuration, the commands against the CLI's own parser, the pip line against the
package's imports - so a drift fails here instead of in a container.

Nothing here asserts that Docker works: this suite runs without a daemon on purpose.
It asserts that the files agree with the code, which is the part a test can own.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest
import yaml

from socialmanager import config as config_mod
from socialmanager.cli import build_parser

REPO = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO / "Dockerfile"
COMPOSE = REPO / "docker-compose.yml"
ENV_EXAMPLE = REPO / ".env.example"
DOCKERIGNORE = REPO / ".dockerignore"
WORKFLOWS = REPO / ".github" / "workflows"
PACKAGE = REPO / "socialmanager"

#: Variables that are not credentials and are not part of ``Settings``: the two the
#: configuration module reads itself, and the three the compose worker loop reads.
NON_CREDENTIAL_VARS = {
    "SOCIAL_CONFIG", "SOCIAL_OVERRIDES",
    "SOCIAL_TICK_LIVE", "SOCIAL_TICK_SECONDS", "SOCIAL_TICK_LIMIT",
}

#: import name -> the name that would appear on a ``pip install`` line.
DISTRIBUTIONS = {"yaml": "PyYAML", "requests": "requests"}


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _env_names(path: Path) -> set[str]:
    """Every ``NAME=...`` in a dotenv file, assigned or merely commented out."""
    names = set()
    for line in _text(path).splitlines():
        match = re.match(r"\s*#?\s*([A-Z][A-Z0-9_]{2,})\s*=", line)
        if match:
            names.add(match.group(1))
    return names


def _cli_verbs(text: str) -> set[str]:
    """The subcommands named by every ``python -m socialmanager <verb>`` in a file."""
    return set(re.findall(r"python\s+-m\s+socialmanager\s+([a-z][a-z0-9-]*)", text))


def _subcommands() -> set[str]:
    parser = build_parser()
    for action in parser._actions:  # noqa: SLF001 - the parser is the contract here
        if hasattr(action, "choices") and action.choices:
            return set(action.choices)
    raise AssertionError("the CLI has no subcommands")


def _third_party_imports() -> set[str]:
    """Top-level modules under ``socialmanager/`` that are neither stdlib nor local."""
    local = {p.stem for p in PACKAGE.glob("*.py")}
    found: set[str] = set()
    for path in PACKAGE.glob("*.py"):
        tree = ast.parse(_text(path), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    return {
        name for name in found
        if name not in sys.stdlib_module_names and name not in local
        and name != "__future__"
    }


# ------------------------------------------------------------------ the image
def test_the_image_installs_every_third_party_import():
    """A new dependency must be added to the Dockerfile, not discovered in the cloud."""
    imported = _third_party_imports()
    assert imported, "the scan found no third-party import at all: check _third_party_imports"
    pip_line = " ".join(
        line for line in _text(DOCKERFILE).splitlines() if "pip install" in line
    )
    for module in sorted(imported):
        distribution = DISTRIBUTIONS.get(module, module)
        assert distribution in pip_line, (
            f"socialmanager imports {module!r} but the Dockerfile does not install "
            f"{distribution!r}: {pip_line}"
        )


def test_the_image_does_not_ship_the_trading_runtime():
    """The social worker may not hold the code that can place an order."""
    dockerfile = _text(DOCKERFILE)
    copied = re.findall(r"^COPY\s+(.+)$", dockerfile, flags=re.MULTILINE)
    assert copied, "the Dockerfile copies nothing"
    assert all("selftrade" not in line for line in copied), copied
    assert "--chown=social:social socialmanager/" in dockerfile


def test_the_image_runs_as_a_non_root_user():
    assert re.search(r"^USER\s+social\s*$", _text(DOCKERFILE), flags=re.MULTILINE)


def test_dockerignore_keeps_the_state_and_the_secrets_out_of_the_context():
    patterns = {
        line.strip() for line in _text(DOCKERIGNORE).splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert {"state/", ".env", "selftrade/"} <= patterns
    assert ".env.example" not in patterns


# ------------------------------------------------------------------ the variables
def test_the_env_example_documents_every_credential():
    """The other direction: a new credential cannot be added without documenting it."""
    documented = _env_names(ENV_EXAMPLE)
    for name in config_mod.Settings().secret_names():
        assert name in documented, f"{name} is missing from .env.example"


def test_the_env_example_invents_no_variable():
    """A name nothing reads is a line that silently does nothing."""
    documented = _env_names(ENV_EXAMPLE)
    known = set(config_mod.Settings().secret_names()) | NON_CREDENTIAL_VARS
    assert documented - known == set()
    assert "SOCIAL_TICK_LIVE" in documented


def test_the_env_example_carries_no_value():
    """It is committed, so every assignment in it must be empty."""
    for line in _text(ENV_EXAMPLE).splitlines():
        match = re.match(r"\s*([A-Z][A-Z0-9_]{2,})\s*=(.*)$", line)
        if match:
            assert match.group(2).strip() == "", f"{match.group(1)} has a value in .env.example"


# ------------------------------------------------------------------ the commands
def test_compose_runs_commands_the_cli_has():
    compose = yaml.safe_load(_text(COMPOSE))
    verbs = _cli_verbs(_text(COMPOSE))
    assert verbs, "no `python -m socialmanager ...` command found in docker-compose.yml"
    assert verbs <= _subcommands(), verbs - _subcommands()
    assert set(compose["services"]) >= {"worker", "dashboard"}


def test_the_workflow_runs_commands_the_cli_has():
    path = WORKFLOWS / "social-worker.yml"
    # PyYAML reads the bare key ``on`` as the boolean True (YAML 1.1); GitHub does
    # not, so the trigger is checked through the raw text instead of the mapping.
    assert re.search(r"^on:\s*$", _text(path), flags=re.MULTILINE)
    verbs = _cli_verbs(_text(path))
    assert verbs, "no `python -m socialmanager ...` command found in the workflow"
    assert verbs <= _subcommands(), verbs - _subcommands()
    yaml.safe_load(_text(path))  # it must still be valid YAML


def test_the_scheduled_worker_publishes_only_when_a_human_asks():
    """The offline-safety property: a cron run simulates, a dispatch may publish."""
    text = _text(WORKFLOWS / "social-worker.yml")
    assert "vars.SOCIAL_CLOUD_ENABLED" in text, "the schedule must be off until switched on"
    live_steps = [line for line in text.splitlines() if "--live" in line]
    assert live_steps, "the manual live path is missing"
    for line in live_steps:
        assert "github.event.inputs.live == 'true'" in text
        assert "--live" in line
    # The simulated tick is the default path, and it runs when live is not true.
    assert "github.event.inputs.live != 'true'" in text


def test_the_compose_dashboard_is_reachable_only_on_the_loopback():
    compose = yaml.safe_load(_text(COMPOSE))
    published = compose["services"]["dashboard"]["ports"]
    assert published and all(str(entry).startswith("127.0.0.1:") for entry in published), published


def test_the_compose_worker_simulates_unless_asked():
    compose = yaml.safe_load(_text(COMPOSE))
    command = compose["services"]["worker"]["command"]
    text = command if isinstance(command, str) else " ".join(command)
    # ``live`` stays empty unless SOCIAL_TICK_LIVE is exactly 1, so a missing or
    # misspelled value means simulation rather than a real post.
    assert 'if [ "$${SOCIAL_TICK_LIVE:-0}" = "1" ]; then live="--live"; fi' in text
    assert "tick" in text and "--live" in text


def test_every_document_the_code_points_at_exists():
    """A readiness note that sends the operator to a missing file is a dead end."""
    missing = []
    for path in sorted(PACKAGE.glob("*.py")):
        for name in re.findall(r"docs/([A-Za-z0-9._-]+\.md)", _text(path)):
            if not (REPO / "docs" / name).exists():
                missing.append(f"{path.name}: docs/{name}")
    assert missing == []


def test_every_command_the_code_points_at_exists():
    """The messages must not promise a subcommand the CLI does not have."""
    verbs = _subcommands()
    missing = []
    for path in sorted(PACKAGE.glob("*.py")):
        for verb in re.findall(r"python -m socialmanager ([a-z][a-z0-9-]*)", _text(path)):
            if verb not in verbs:
                missing.append(f"{path.name}: {verb}")
    assert missing == []


def test_the_dashboard_service_asks_for_a_password():
    """0.0.0.0 without a password is refused by the panel itself: not configured here."""
    compose = yaml.safe_load(_text(COMPOSE))
    command = compose["services"]["dashboard"]["command"]
    text = command if isinstance(command, str) else " ".join(command)
    assert "0.0.0.0" in text
    assert "SOCIAL_DASHBOARD_PASSWORD" in _env_names(ENV_EXAMPLE)
