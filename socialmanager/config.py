"""Configuration: one YAML file, names-not-values, conservative defaults everywhere.

The file (``social.yaml``) is the operator's contract.  Three rules:

* **everything has a safe default**, so the tool runs on a fresh clone with no
  file at all - and the default is *draft mode, connectors disabled*;
* **a secret is a variable name**, validated by
  :func:`socialmanager.secrets.check_env_name`, so a pasted token is a config
  error and not a leaked credential;
* **the file can be overridden per run** with ``SOCIAL_OVERRIDES``
  (``autonomy.mode=controlled,dashboard.port=9000``), which is how the cloud
  worker changes behaviour without a second config file, and how the tests
  exercise limits without editing anything on disk.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml

from . import secrets
from .models import AutonomyMode, Category, LANGS, Platform

CONFIG_ENV = "SOCIAL_CONFIG"
OVERRIDES_ENV = "SOCIAL_OVERRIDES"
DEFAULT_CONFIG_PATH = "social.yaml"
DEFAULT_DB = "state/social.db"


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "si", "sì"}


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class ProjectCfg:
    name: str = "selftrade"
    repo: str = "SalvoG1234/selftrade"
    repo_url: str = "https://github.com/SalvoG1234/selftrade"
    brand: str = "SelfTrade"
    tagline: str = "An open, self-learning trading research loop"
    docs_url: str = "https://github.com/SalvoG1234/selftrade/tree/master/docs"
    timezone: str = "Europe/Rome"
    locale_note: str = ""     # free text shown next to generated drafts


@dataclass
class DatabaseCfg:
    url: str = DEFAULT_DB           # sqlite path, or postgresql://... in the cloud
    timeout: float = 30.0
    backup_dir: str = "state/backups"
    retention_days: dict[str, int] = field(
        default_factory=lambda: {"api_call": 30, "agent_run": 90, "audit_event": 0}
    )


@dataclass
class GithubCfg:
    enabled: bool = True
    token_env: str = "SOCIAL_GITHUB_TOKEN"     # optional: public repo reads work without it
    api_base: str = "https://api.github.com"
    timeout: float = 20.0
    per_page: int = 30
    releases_limit: int = 5
    commits_limit: int = 20
    issues_limit: int = 20
    discussions: bool = False                  # needs a token with discussions read
    include_readme: bool = True
    since_days: int = 30
    user_agent: str = "selftrade-social-manager"


@dataclass
class XCfg:
    enabled: bool = False
    dry_run: bool = True
    api_base: str = "https://api.x.com"
    account_handle: str = ""
    client_id_env: str = "SOCIAL_X_CLIENT_ID"
    client_secret_env: str = "SOCIAL_X_CLIENT_SECRET"
    access_token_env: str = "SOCIAL_X_ACCESS_TOKEN"
    refresh_token_env: str = "SOCIAL_X_REFRESH_TOKEN"
    oauth_authorize_url: str = "https://x.com/i/oauth2/authorize"
    oauth_token_url: str = "https://api.x.com/2/oauth2/token"
    scopes: list[str] = field(
        default_factory=lambda: [
            "tweet.read",
            "tweet.write",
            "users.read",
            "media.write",
            "offline.access",
        ]
    )
    daily_post_limit: int = 3
    timeout: float = 30.0


@dataclass
class InstagramCfg:
    enabled: bool = False
    dry_run: bool = True
    #: ``instagram_login`` (no Facebook Page) or ``facebook_login`` (Page-linked).
    login: str = "instagram_login"
    graph_version: str = "v25.0"
    ig_user_id: str = ""
    account_handle: str = ""
    token_env: str = "SOCIAL_IG_ACCESS_TOKEN"
    app_secret_env: str = "SOCIAL_IG_APP_SECRET"
    api_base: str = "https://graph.instagram.com"
    api_base_facebook_login: str = "https://graph.facebook.com"
    public_media_base_url: str = ""   # Meta fetches media over HTTP; must be public
    media_dir: str = "state/media"
    daily_post_limit: int = 1
    container_poll_s: float = 3.0
    container_poll_max: int = 30
    timeout: float = 60.0


@dataclass
class LlmCfg:
    enabled: bool = True
    provider: str = "ollama"          # ollama | openai_compatible | none
    base_url: str = "http://localhost:11434"
    model: str = "qwen2.5-coder:1.5b"
    api_key_env: str = "SOCIAL_LLM_API_KEY"
    temperature: float = 0.4
    num_ctx: int = 8192
    num_predict: int = 900
    timeout: float = 180.0
    #: A remote provider is used **only** when explicitly allowed and configured.
    allow_remote_fallback: bool = False
    remote_base_url: str = ""
    remote_model: str = ""
    remote_api_key_env: str = "SOCIAL_LLM_REMOTE_API_KEY"


@dataclass
class EditorialCfg:
    languages: list[str] = field(default_factory=lambda: list(LANGS))
    categories: list[str] = field(
        default_factory=lambda: [str(c) for c in Category]
    )
    weekly_targets: dict[str, int] = field(
        default_factory=lambda: {str(Platform.X): 3, str(Platform.INSTAGRAM): 2, str(Platform.GITHUB): 1}
    )
    max_posts_per_day: dict[str, int] = field(
        default_factory=lambda: {str(Platform.X): 2, str(Platform.INSTAGRAM): 1, str(Platform.GITHUB): 1}
    )
    min_gap_minutes: int = 90
    planning_days: int = 7
    quiet_hours: list[int] = field(default_factory=lambda: [23, 7])  # start, end (local)
    default_slots: dict[str, list[str]] = field(
        default_factory=lambda: {
            str(Platform.X): ["09:30", "18:00"],
            str(Platform.INSTAGRAM): ["12:30"],
            str(Platform.GITHUB): ["17:00"],
        }
    )
    #: A category may not exceed this share of a week's slots.
    max_category_share: float = 0.35


@dataclass
class AutonomyCfg:
    mode: str = str(AutonomyMode.DRAFT)
    kill_switch: bool = False
    allowed_platforms: list[str] = field(
        default_factory=lambda: [str(p) for p in Platform]
    )
    max_posts_per_run: int = 3
    allow_replies: bool = False          # sending replies/comments stays off by default
    allow_issue_proposals: bool = False  # proposing issues stays off by default
    require_approval_risks: list[str] = field(
        default_factory=lambda: ["high", "sensitive"]
    )


@dataclass
class PolicyCfg:
    min_chars: dict[str, int] = field(
        default_factory=lambda: {str(Platform.X): 40, str(Platform.INSTAGRAM): 30, str(Platform.GITHUB): 40}
    )
    require_sources: bool = True
    max_links_per_post: int = 1
    max_hashtags: dict[str, int] = field(
        default_factory=lambda: {str(Platform.X): 3, str(Platform.INSTAGRAM): 15}
    )
    max_emojis: int = 12
    banned_phrases: list[str] = field(
        default_factory=lambda: [
            "guaranteed profit",
            "guaranteed returns",
            "profitto garantito",
            "rendimento garantito",
            "risk free",
            "senza rischio",
            "get rich",
            "soldi facili",
            "double your money",
            "to the moon",
            "100% accurate",
            "100% accurato",
            "beat the market",
            "battere il mercato",
        ]
    )
    #: Terms that force a human decision instead of a block (context can be fine):
    #: performance, returns, money, live results.
    review_terms: list[str] = field(
        default_factory=lambda: [
            "profit", "profitto", "rendimento", "return", "yield", "roi",
            "earnings", "guadagno", "performance", "sharpe", "drawdown",
            "backtest", "live trading", "pnl", "capitale", "capital",
        ]
    )
    #: Patterns that must never appear in a post body (privacy / leakage).
    pii_patterns: list[str] = field(
        default_factory=lambda: [
            r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b",              # an email address
            r"\b(?:\d[ -]?){13,19}\b",                       # a card-like number
            r"\b(?:sk-|gh[pousr]_|xox[baprs]-)[A-Za-z0-9_\-]{10,}\b",  # a token
            r"\b(?:\+?\d{1,3}[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}\b",  # a phone number
        ]
    )
    review_without_sources: bool = True
    source_hosts_allowed: list[str] = field(
        default_factory=lambda: ["github.com", "raw.githubusercontent.com"]
    )


@dataclass
class DashboardCfg:
    host: str = "127.0.0.1"
    port: int = 8788
    auth: bool = True
    password_env: str = "SOCIAL_DASHBOARD_PASSWORD"
    password_file: str = "state/social_dashboard_auth.json"
    session_ttl_s: int = 86400
    password_min_length: int = 8
    auto_refresh_s: int = 30
    page_size: int = 25


@dataclass
class NotifyCfg:
    enabled: bool = False
    telegram_token_env: str = "SOCIAL_TELEGRAM_TOKEN"
    telegram_chat_id_env: str = "SOCIAL_TELEGRAM_CHAT_ID"
    on_error: bool = True
    on_approval_request: bool = True
    on_policy_block: bool = False


@dataclass
class Settings:
    """The whole configuration, plus where it came from and what was overridden."""

    project: ProjectCfg = field(default_factory=ProjectCfg)
    database: DatabaseCfg = field(default_factory=DatabaseCfg)
    github: GithubCfg = field(default_factory=GithubCfg)
    x: XCfg = field(default_factory=XCfg)
    instagram: InstagramCfg = field(default_factory=InstagramCfg)
    llm: LlmCfg = field(default_factory=LlmCfg)
    editorial: EditorialCfg = field(default_factory=EditorialCfg)
    autonomy: AutonomyCfg = field(default_factory=AutonomyCfg)
    policy: PolicyCfg = field(default_factory=PolicyCfg)
    dashboard: DashboardCfg = field(default_factory=DashboardCfg)
    notify: NotifyCfg = field(default_factory=NotifyCfg)
    path: str = ""
    existed: bool = False
    overrides: list[str] = field(default_factory=list)
    unknown_keys: list[str] = field(default_factory=list)

    # ---------------------------------------------------------------- helpers
    def secret_names(self) -> list[str]:
        """Every environment variable name configuration refers to."""
        names = [
            self.github.token_env,
            self.x.client_id_env,
            self.x.client_secret_env,
            self.x.access_token_env,
            self.x.refresh_token_env,
            self.instagram.token_env,
            self.instagram.app_secret_env,
            self.llm.api_key_env,
            self.llm.remote_api_key_env,
            self.dashboard.password_env,
            self.notify.telegram_token_env,
            self.notify.telegram_chat_id_env,
        ]
        return [n for n in names if n]

    def db_path(self) -> Path:
        return Path(self.database.url)

    def state_dir(self) -> Path:
        return self.db_path().parent

    def mode(self) -> AutonomyMode:
        return AutonomyMode(str(self.autonomy.mode))

    def platforms(self) -> list[Platform]:
        return [Platform(p) for p in self.autonomy.allowed_platforms]

    def is_postgres(self) -> bool:
        return str(self.database.url).startswith(("postgres://", "postgresql://"))

    def to_dict(self) -> dict[str, Any]:
        """A JSON-safe view for the dashboard: names, never values."""

        def convert(value: Any) -> Any:
            if is_dataclass(value):
                return {f.name: convert(getattr(value, f.name)) for f in fields(value)}
            if isinstance(value, dict):
                return {str(k): convert(v) for k, v in value.items()}
            if isinstance(value, (list, tuple)):
                return [convert(v) for v in value]
            if isinstance(value, Path):
                return str(value)
            return value

        data = {f.name: convert(getattr(self, f.name)) for f in fields(self)}
        data["env_present"] = {
            name: secrets.present(name) for name in self.secret_names()
        }
        return data


# ---------------------------------------------------------------------- loading
def _fill(obj: Any, data: dict[str, Any], prefix: str, unknown: list[str]) -> None:
    known = {f.name: f for f in fields(obj)}
    for key, value in data.items():
        if key not in known:
            unknown.append(f"{prefix}{key}")
            continue
        current = getattr(obj, key)
        if is_dataclass(current) and isinstance(value, dict):
            _fill(current, value, f"{prefix}{key}.", unknown)
        elif is_dataclass(current) and value is not None:
            # Replacing a whole section with a scalar would silently break every
            # reader further down; refuse it here instead.
            raise ValueError(
                f"{prefix}{key} must be a mapping of settings, not {type(value).__name__}"
            )
        elif isinstance(current, dict) and isinstance(value, dict):
            setattr(obj, key, {**current, **{str(k): v for k, v in value.items()}})
        elif current is None or value is not None:
            setattr(obj, key, value)


def _coerce(settings: Settings) -> None:
    """Force the types the rest of the code relies on."""
    s = settings
    s.database.timeout = _as_float(s.database.timeout, 30.0)
    s.database.retention_days = {
        str(k): _as_int(v, 0) for k, v in _as_dict(s.database.retention_days).items()
    }
    s.github.enabled = _as_bool(s.github.enabled, True)
    s.github.timeout = _as_float(s.github.timeout, 20.0)
    s.github.per_page = min(100, max(1, _as_int(s.github.per_page, 30)))
    s.github.releases_limit = max(0, _as_int(s.github.releases_limit, 5))
    s.github.commits_limit = max(0, _as_int(s.github.commits_limit, 20))
    s.github.issues_limit = max(0, _as_int(s.github.issues_limit, 20))
    s.github.since_days = max(1, _as_int(s.github.since_days, 30))
    s.github.discussions = _as_bool(s.github.discussions, False)
    s.github.include_readme = _as_bool(s.github.include_readme, True)
    for cfg in (s.x, s.instagram):
        cfg.enabled = _as_bool(cfg.enabled, False)
        cfg.dry_run = _as_bool(cfg.dry_run, True)
    s.x.daily_post_limit = max(0, _as_int(s.x.daily_post_limit, 3))
    s.instagram.daily_post_limit = max(0, _as_int(s.instagram.daily_post_limit, 1))
    s.instagram.graph_version = str(s.instagram.graph_version or "v25.0").lstrip("v")
    s.instagram.graph_version = "v" + s.instagram.graph_version
    s.llm.enabled = _as_bool(s.llm.enabled, True)
    s.llm.allow_remote_fallback = _as_bool(s.llm.allow_remote_fallback, False)
    s.llm.temperature = _as_float(s.llm.temperature, 0.4)
    s.llm.num_ctx = _as_int(s.llm.num_ctx, 8192)
    s.llm.num_predict = _as_int(s.llm.num_predict, 900)
    s.llm.timeout = _as_float(s.llm.timeout, 180.0)
    s.editorial.languages = [str(x) for x in _as_list(s.editorial.languages)] or list(LANGS)
    s.editorial.categories = [str(x) for x in _as_list(s.editorial.categories)] or [
        str(c) for c in Category
    ]
    s.editorial.weekly_targets = {
        str(k): _as_int(v, 0) for k, v in _as_dict(s.editorial.weekly_targets).items()
    }
    s.editorial.max_posts_per_day = {
        str(k): _as_int(v, 0) for k, v in _as_dict(s.editorial.max_posts_per_day).items()
    }
    s.editorial.min_gap_minutes = max(0, _as_int(s.editorial.min_gap_minutes, 90))
    s.editorial.planning_days = max(1, _as_int(s.editorial.planning_days, 7))
    s.editorial.max_category_share = min(
        1.0, max(0.05, _as_float(s.editorial.max_category_share, 0.35))
    )
    s.autonomy.mode = str(s.autonomy.mode or AutonomyMode.DRAFT)
    s.autonomy.kill_switch = _as_bool(s.autonomy.kill_switch, False)
    s.autonomy.allowed_platforms = [str(x) for x in _as_list(s.autonomy.allowed_platforms)] or [
        str(p) for p in Platform
    ]
    s.autonomy.max_posts_per_run = max(0, _as_int(s.autonomy.max_posts_per_run, 3))
    s.autonomy.allow_replies = _as_bool(s.autonomy.allow_replies, False)
    s.autonomy.allow_issue_proposals = _as_bool(s.autonomy.allow_issue_proposals, False)
    s.autonomy.require_approval_risks = [
        str(x) for x in _as_list(s.autonomy.require_approval_risks)
    ] or ["high", "sensitive"]
    s.policy.require_sources = _as_bool(s.policy.require_sources, True)
    s.policy.max_links_per_post = max(0, _as_int(s.policy.max_links_per_post, 1))
    s.policy.max_emojis = max(0, _as_int(s.policy.max_emojis, 12))
    s.policy.banned_phrases = [str(x) for x in _as_list(s.policy.banned_phrases)]
    s.policy.review_terms = [str(x) for x in _as_list(s.policy.review_terms)]
    s.policy.pii_patterns = [str(x) for x in _as_list(s.policy.pii_patterns)]
    s.policy.min_chars = {str(k): _as_int(v, 0) for k, v in _as_dict(s.policy.min_chars).items()}
    s.policy.max_hashtags = {
        str(k): _as_int(v, 0) for k, v in _as_dict(s.policy.max_hashtags).items()
    }
    s.policy.source_hosts_allowed = [
        str(x) for x in _as_list(s.policy.source_hosts_allowed)
    ]
    s.dashboard.port = max(1, min(65535, _as_int(s.dashboard.port, 8788)))
    s.dashboard.auth = _as_bool(s.dashboard.auth, True)
    s.dashboard.session_ttl_s = max(60, _as_int(s.dashboard.session_ttl_s, 86400))
    s.dashboard.password_min_length = max(4, _as_int(s.dashboard.password_min_length, 8))
    s.dashboard.auto_refresh_s = max(0, _as_int(s.dashboard.auto_refresh_s, 30))
    s.dashboard.page_size = max(5, _as_int(s.dashboard.page_size, 25))
    s.notify.enabled = _as_bool(s.notify.enabled, False)
    s.notify.on_error = _as_bool(s.notify.on_error, True)
    s.notify.on_approval_request = _as_bool(s.notify.on_approval_request, True)
    s.notify.on_policy_block = _as_bool(s.notify.on_policy_block, False)


def _parse_overrides(raw: str) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    for chunk in str(raw or "").split(","):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        out.append((key.strip(), yaml.safe_load(value.strip())))
    return out


def _apply_override(settings: Settings, dotted: str, value: Any) -> bool:
    parts = dotted.split(".")
    target: Any = settings
    for part in parts[:-1]:
        if not hasattr(target, part):
            return False
        target = getattr(target, part)
        if not is_dataclass(target) and not isinstance(target, dict):
            return False
    leaf = parts[-1]
    if isinstance(target, dict):
        target[leaf] = value
        return True
    if not hasattr(target, leaf):
        return False
    setattr(target, leaf, value)
    return True


def load(path: str | Path | None = None, overrides: str | None = None) -> Settings:
    """Read the config file (if any), apply overrides, and coerce the types."""
    resolved = Path(
        str(path)
        or os.environ.get(CONFIG_ENV)
        or DEFAULT_CONFIG_PATH
    )
    settings = Settings(path=str(resolved))
    if resolved.exists():
        try:
            data = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:  # a broken config must not be silent
            raise ValueError(f"cannot read {resolved}: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"{resolved} must contain a YAML mapping")
        _fill(settings, data, "", settings.unknown_keys)
        settings.existed = True

    raw_overrides = overrides if overrides is not None else os.environ.get(OVERRIDES_ENV, "")
    for key, value in _parse_overrides(raw_overrides or ""):
        if _apply_override(settings, key, value):
            settings.overrides.append(f"{key}={value!r}")
        else:
            settings.unknown_keys.append(key)

    _coerce(settings)
    return settings


def validate(settings: Settings) -> list[str]:
    """Everything wrong with a configuration, as human-readable lines."""
    problems: list[str] = []
    for attr in ("project", "database", "github", "x", "instagram", "llm",
                 "editorial", "autonomy", "policy", "dashboard", "notify"):
        section = getattr(settings, attr)
        for f in fields(section):
            value = getattr(section, f.name)
            if f.name.endswith("_env"):
                problem = secrets.check_env_name(value)
                if problem:
                    problems.append(f"{attr}.{f.name}: {problem}")
    try:
        AutonomyMode(str(settings.autonomy.mode))
    except ValueError:
        problems.append(
            f"autonomy.mode: {settings.autonomy.mode!r} is not one of "
            + ", ".join(str(m) for m in AutonomyMode)
        )
    for lang in settings.editorial.languages:
        if lang not in LANGS:
            problems.append(f"editorial.languages: {lang!r} is not supported ({', '.join(LANGS)})")
    known_platforms = {str(p) for p in Platform}
    for platform in settings.autonomy.allowed_platforms:
        if platform not in known_platforms:
            problems.append(f"autonomy.allowed_platforms: unknown platform {platform!r}")
    if not settings.database.url:
        problems.append("database.url: empty")
    if settings.is_postgres():
        problems.append(
            "database.url: PostgreSQL needs an installed driver (psycopg); this build "
            "ships SQLite only - see docs/SOCIAL-DEPLOY.md"
        )
    if settings.dashboard.host not in {"127.0.0.1", "localhost", "::1"}:
        if not settings.dashboard.auth:
            problems.append(
                "dashboard: a non-loopback bind with dashboard.auth=false is refused"
            )
        if secrets.present(settings.dashboard.password_env) is False and not Path(
            settings.dashboard.password_file
        ).exists():
            problems.append(
                "dashboard: a non-loopback bind needs a password "
                f"(set {settings.dashboard.password_env} or change the stored one first)"
            )
    if settings.instagram.public_media_base_url and not settings.instagram.public_media_base_url.startswith("https://"):
        problems.append("instagram.public_media_base_url must be https (Meta fetches it)")
    for key in settings.unknown_keys:
        problems.append(f"unknown configuration key: {key}")
    return problems


def dumps(settings: Settings) -> str:
    """The effective configuration as YAML, for ``config show`` and the audit log."""
    data = settings.to_dict()
    for key in ("path", "existed", "overrides", "unknown_keys", "env_present"):
        data.pop(key, None)
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
