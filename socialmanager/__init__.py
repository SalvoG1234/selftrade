"""SelfTrade Social Manager - a modular monolith that promotes the `selftrade` repo.

The package is split along the boundaries of the mission brief, and the split is
enforced by *interfaces*, not by convention:

* :mod:`socialmanager.github`   - read-only GitHub connector (Phase 2);
* :mod:`socialmanager.content`  - bilingual (it/en) draft generation (Phase 2);
* :mod:`socialmanager.planner`  - editorial calendar and budgeting (Phase 2);
* :mod:`socialmanager.policy`   - the deterministic gate every action passes;
* :mod:`socialmanager.approvals`- the human decision queue;
* :mod:`socialmanager.publisher`- the *only* module allowed to talk to a platform;
* :mod:`socialmanager.community`- feedback ingestion and classification (Phase 6);
* :mod:`socialmanager.analytics`- metrics and reports (Phase 6);
* :mod:`socialmanager.db`       - SQLite/PostgreSQL-shaped persistence + audit log;
* :mod:`socialmanager.dashboard`- the admin UI and its JSON API.

No LLM ever sees a token: agents produce *structured proposals*
(:class:`socialmanager.models.ContentIdea`), and only the publisher, after the
policy engine and the approval manager have both said yes, may turn one into an
API call.  The credentials themselves live in the environment, are referred to
by *name* in configuration, and are never logged or returned by the API.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
