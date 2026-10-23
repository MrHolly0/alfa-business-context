"""Versioned analyst configurations and audited scope activation."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .schemas import StrictModel


class _FrozenList(list):
    """JSON-list compatible collection that cannot be mutated after validation."""
    def _immutable(self, *args, **kwargs): raise TypeError("analyst config collections are immutable")
    __setitem__ = __delitem__ = append = clear = extend = insert = pop = remove = reverse = sort = __iadd__ = __imul__ = _immutable


def _industry_key(value: str) -> str:
    return value.strip().casefold().replace(":", "-")


class AnalystConfig(StrictModel, frozen=True):
    config_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    revision: int = Field(default=1, ge=1)
    template_version: Literal["analysis-v2"] = "analysis-v2"
    focus: list[Literal["costs", "liquidity", "supply", "regulation", "growth", "uncertainty"]] = Field(default_factory=list, max_length=4)
    retrieval_strategy: Literal["event_first", "dependencies_first", "balanced"] = "balanced"
    answer_style: Literal["concise", "balanced", "detailed"] = "balanced"
    horizon: Literal["short", "quarter", "long"] = "quarter"
    role_scope: Literal["any", "finance", "procurement", "executive"] = "any"
    industry_scope: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(default_factory=list, max_length=10)
    evidence_limit: int = Field(default=16, ge=4, le=16)

    @field_validator("industry_scope")
    @classmethod
    def normalize_industries(cls, values):
        normalized = [_industry_key(value) for value in values]
        if any(not value for value in normalized) or len(set(normalized)) != len(normalized):
            raise ValueError("industry_scope values must be unique and non-empty")
        return normalized

    @model_validator(mode="after")
    def freeze_collections(self):
        object.__setattr__(self, "focus", _FrozenList(self.focus))
        object.__setattr__(self, "industry_scope", _FrozenList(self.industry_scope))
        return self

    @property
    def ref(self) -> str:
        return f"{self.config_id}@{self.revision}"


def builtin_configs() -> list[AnalystConfig]:
    return [AnalystConfig(config_id="baseline"),
        AnalystConfig(config_id="finance", focus=["costs", "liquidity", "uncertainty"], role_scope="finance", answer_style="detailed"),
        AnalystConfig(config_id="procurement", focus=["supply", "costs", "regulation"], role_scope="procurement", retrieval_strategy="dependencies_first"),
        AnalystConfig(config_id="executive", focus=["growth", "regulation", "uncertainty"], role_scope="executive", answer_style="concise")]


def _dump(c: AnalystConfig) -> dict:
    return c.model_dump(mode="json")


def _role_bucket(role: str) -> str:
    value = role.casefold()
    if any(x in value for x in ("финанс", "finance", "financial", "cfo", "казнач")): return "finance"
    if any(x in value for x in ("закуп", "procurement", "снабжен")): return "procurement"
    if any(x in value for x in ("директор", "руковод", "ceo", "генеральн", "executive")): return "executive"
    return "any"


class PromptRegistry:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS configs(ref TEXT PRIMARY KEY, config_json TEXT NOT NULL, content_hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS activations(scope TEXT NOT NULL, revision INTEGER NOT NULL, ref TEXT, reviewer TEXT NOT NULL, report_digest TEXT, action TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(scope,revision));""")
        if not self.list():
            for config in builtin_configs(): self.register(config)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level="IMMEDIATE")
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback(); raise
        finally: db.close()

    def register(self, config):
        c = config if isinstance(config, AnalystConfig) else AnalystConfig.model_validate(config)
        body = _dump(c); raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT config_json,content_hash FROM configs WHERE ref=?", (c.ref,)).fetchone()
            if row and row["content_hash"] != digest: raise ValueError("config revision is immutable")
            db.execute("INSERT OR IGNORE INTO configs VALUES(?,?,?)", (c.ref, raw, digest))
        return {"ref": c.ref, "content_hash": digest}

    def get(self, ref):
        with self._db() as db: row = db.execute("SELECT config_json FROM configs WHERE ref=?", (ref,)).fetchone()
        if not row: raise KeyError(ref)
        return AnalystConfig.model_validate_json(row["config_json"])

    def list(self):
        with self._db() as db: rows = db.execute("SELECT ref,content_hash FROM configs ORDER BY ref").fetchall()
        return [{"ref": r["ref"], "content_hash": r["content_hash"]} for r in rows]

    @staticmethod
    def scope(role, industry):
        bucket = role if role in {"any", "finance", "procurement", "executive"} else _role_bucket(role)
        industry_key = _industry_key(industry) if industry and industry != "*" else "*"
        return f"impact:{bucket}:{industry_key}"

    def select(self, *, role, industry, task="impact"):
        if task != "impact": return self.get("baseline@1")
        bucket = _role_bucket(role)
        scopes = [self.scope(bucket, industry), self.scope(bucket, "*")]
        if bucket != "any": scopes.extend([self.scope("any", industry), self.scope("any", "*")])
        with self._db() as db:
            for scope in scopes:
                row = db.execute("SELECT ref FROM activations WHERE scope=? ORDER BY revision DESC LIMIT 1", (scope,)).fetchone()
                if row and row["ref"]: return self.get(row["ref"])
        return self.get("baseline@1")

    def _activate(self, scope, ref, reviewer, action, report_digest, expected_revision):
        if not isinstance(reviewer, str) or not reviewer.strip() or len(reviewer) > 128: raise ValueError("reviewer must be non-empty and at most 128 characters")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0: raise ValueError("expected_revision must be a non-negative integer")
        if not isinstance(scope, str) or not re.fullmatch(r"impact:(any|finance|procurement|executive):([^:]+|\*)", scope, flags=re.IGNORECASE): raise ValueError("invalid scope")
        scope_role, scope_industry = scope.split(":")[1:]
        scope = f"impact:{scope_role.casefold()}:{_industry_key(scope_industry) if scope_industry != '*' else '*'}"
        config = self.get(ref) if ref else None
        role, industry = scope.split(":")[1:]
        if config and config.role_scope not in ("any", role): raise ValueError("config role scope does not match activation")
        if config and config.industry_scope and (industry == "*" or _industry_key(industry) not in config.industry_scope): raise ValueError("config industry scope does not match activation")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT COALESCE(MAX(revision),0) n FROM activations WHERE scope=?", (scope,)).fetchone(); current=row["n"]
            if current != expected_revision: raise ValueError("activation revision conflict")
            revision=current+1
            db.execute("INSERT INTO activations(scope,revision,ref,reviewer,report_digest,action) VALUES(?,?,?,?,?,?)",(scope,revision,ref,reviewer,report_digest,action))
        return {"scope":scope,"revision":revision,"ref":ref,"reviewer":reviewer,"report_digest":report_digest,"action":action}

    def promote(self, *, scope, ref, report: dict, reviewer, expected_revision=0):
        if (report.get("schema_version")!="tournament-report-v1" or report.get("winner_ref")!=ref or report.get("mode")!="model" or report.get("shadow") is not False or report.get("promotion_eligible") is not True or report.get("synthetic") is not False or report.get("holdout",{}).get("passed") is not True or report.get("human_review",{}).get("approved") is not True or report.get("human_review",{}).get("reviewer")!=reviewer):
            raise ValueError("promotion report gates not satisfied")
        return self._activate(scope,ref,reviewer,"promote",hashlib.sha256(json.dumps(report,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest(),expected_revision)

    def rollback(self, *, scope, reviewer, expected_revision):
        with self._db() as db:
            current=db.execute("SELECT MAX(revision) n FROM activations WHERE scope=?",(scope,)).fetchone()["n"] or 0
            previous=db.execute("SELECT ref FROM activations WHERE scope=? AND revision<? AND ref IS NOT NULL ORDER BY revision DESC LIMIT 1",(scope,current)).fetchone()
        if current != expected_revision: raise ValueError("activation revision conflict")
        return self._activate(scope, previous["ref"] if previous else "baseline@1", reviewer,"rollback",None,expected_revision)
