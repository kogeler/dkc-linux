"""Pure discovery decision logic for the unattended repository lifecycle."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from .debver import DebianVersion, compare
from .records import DiscoveryDecision, LtoMode
from .retention import RetentionMode
from .state import AuthoritativeState

__all__ = ["decide"]


def _utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("decision clock must be timezone-aware")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _valid_until(state: AuthoritativeState) -> datetime:
    raw = state.manifest.apt_metadata.get("valid_until")
    if not isinstance(raw, str):
        raise ValueError("authoritative manifest has no Valid-Until timestamp")
    parsed = parsedate_to_datetime(raw)
    if parsed.tzinfo is None:
        raise ValueError("authoritative Valid-Until lacks a timezone")
    return parsed.astimezone(timezone.utc)


def decide(
    *,
    source_version: str,
    source_dsc_sha256: str,
    dkc_revision: int,
    build_policy_sha256: str,
    lto_mode: LtoMode,
    retention_mode: RetentionMode,
    retention_max_bytes: int | None,
    now: datetime,
    state: AuthoritativeState | None,
    state_read_succeeded: bool,
    bootstrap_allowed: bool,
    maintenance_horizon_seconds: int = 7 * 86400,
) -> DiscoveryDecision:
    """Return one typed, fail-closed lifecycle decision.

    A missing object is an authoritative bootstrap fact only when the read
    itself succeeded. An unavailable read is never treated as an empty bucket.
    """
    now_utc = _utc(now)
    source = DebianVersion.parse(source_version)
    if maintenance_horizon_seconds < 3600:
        raise ValueError("maintenance horizon must be at least one hour")
    if retention_mode == "series":
        if retention_max_bytes is not None:
            raise ValueError("series retention must not carry a byte limit")
    elif retention_mode == "series-size":
        if retention_max_bytes is None or retention_max_bytes < 1:
            raise ValueError("series-size retention requires a positive byte limit")
    else:
        raise ValueError("unknown retention mode")
    retention_fields: dict[str, Any] = {
        "retention_mode": retention_mode,
        "retention_max_bytes": retention_max_bytes,
    }
    if not state_read_succeeded:
        return DiscoveryDecision(
            decision="blocked",
            source_version=source_version,
            source_dsc_sha256=source_dsc_sha256,
            dkc_revision=dkc_revision,
            build_policy_sha256=build_policy_sha256,
            lto_mode=lto_mode,
            **retention_fields,
            utc=now_utc,
            reason="authoritative state could not be read",
        )
    if state is None:
        if not bootstrap_allowed:
            return DiscoveryDecision(
                decision="blocked",
                source_version=source_version,
                source_dsc_sha256=source_dsc_sha256,
                dkc_revision=dkc_revision,
                build_policy_sha256=build_policy_sha256,
                lto_mode=lto_mode,
                **retention_fields,
                utc=now_utc,
                authoritative_state_read=True,
                reason="authoritative state is absent and bootstrap is not allowed",
            )
        return DiscoveryDecision(
            decision="build",
            source_version=source_version,
            source_dsc_sha256=source_dsc_sha256,
            dkc_revision=dkc_revision,
            build_policy_sha256=build_policy_sha256,
            lto_mode=lto_mode,
            **retention_fields,
            utc=now_utc,
            build_required=True,
            publish_allowed=True,
            authoritative_state_read=True,
            reason="bootstrap an empty repository",
        )

    published = DebianVersion.parse(state.manifest.source_version)
    ordering = compare(source, published)
    if ordering < 0:
        return DiscoveryDecision(
            decision="blocked",
            source_version=source_version,
            source_dsc_sha256=source_dsc_sha256,
            dkc_revision=dkc_revision,
            build_policy_sha256=build_policy_sha256,
            lto_mode=lto_mode,
            **retention_fields,
            utc=now_utc,
            authoritative_state_read=True,
            state_generation=state.pointer.generation,
            state_publication_id=state.pointer.publication_id,
            reason="the authenticated source archive is older than the publication",
        )
    if ordering == 0 and source_dsc_sha256 != state.manifest.source_dsc_sha256:
        return DiscoveryDecision(
            decision="blocked",
            source_version=source_version,
            source_dsc_sha256=source_dsc_sha256,
            dkc_revision=dkc_revision,
            build_policy_sha256=build_policy_sha256,
            lto_mode=lto_mode,
            **retention_fields,
            utc=now_utc,
            authoritative_state_read=True,
            state_generation=state.pointer.generation,
            state_publication_id=state.pointer.publication_id,
            reason="the published source version now has a different descriptor hash",
        )
    if ordering > 0:
        return DiscoveryDecision(
            decision="build",
            source_version=source_version,
            source_dsc_sha256=source_dsc_sha256,
            dkc_revision=dkc_revision,
            build_policy_sha256=build_policy_sha256,
            lto_mode=lto_mode,
            **retention_fields,
            utc=now_utc,
            build_required=True,
            publish_allowed=True,
            authoritative_state_read=True,
            state_generation=state.pointer.generation,
            state_publication_id=state.pointer.publication_id,
            reason="a newer authenticated source is available",
        )

    if dkc_revision < state.manifest.dkc_revision:
        return DiscoveryDecision(
            decision="blocked",
            source_version=source_version,
            source_dsc_sha256=source_dsc_sha256,
            dkc_revision=dkc_revision,
            build_policy_sha256=build_policy_sha256,
            lto_mode=lto_mode,
            **retention_fields,
            utc=now_utc,
            authoritative_state_read=True,
            state_generation=state.pointer.generation,
            state_publication_id=state.pointer.publication_id,
            reason="the configured downstream revision is older than the publication",
        )
    if dkc_revision > state.manifest.dkc_revision:
        return DiscoveryDecision(
            decision="build",
            source_version=source_version,
            source_dsc_sha256=source_dsc_sha256,
            dkc_revision=dkc_revision,
            build_policy_sha256=build_policy_sha256,
            lto_mode=lto_mode,
            **retention_fields,
            utc=now_utc,
            build_required=True,
            publish_allowed=True,
            authoritative_state_read=True,
            state_generation=state.pointer.generation,
            state_publication_id=state.pointer.publication_id,
            reason="a higher downstream revision is configured",
        )
    if (
        build_policy_sha256 != state.manifest.build_policy_sha256
        or lto_mode != state.manifest.lto_mode
    ):
        return DiscoveryDecision(
            decision="blocked",
            source_version=source_version,
            source_dsc_sha256=source_dsc_sha256,
            dkc_revision=dkc_revision,
            build_policy_sha256=build_policy_sha256,
            lto_mode=lto_mode,
            **retention_fields,
            utc=now_utc,
            authoritative_state_read=True,
            state_generation=state.pointer.generation,
            state_publication_id=state.pointer.publication_id,
            reason="build policy changed without a downstream revision increase",
        )

    if (
        retention_mode != state.manifest.retention_mode
        or retention_max_bytes != state.manifest.retention_max_bytes
        or (
            retention_mode == "series-size"
            and retention_max_bytes is not None
            and state.storage_size > retention_max_bytes
        )
    ):
        return DiscoveryDecision(
            decision="maintenance",
            source_version=source_version,
            source_dsc_sha256=source_dsc_sha256,
            dkc_revision=dkc_revision,
            build_policy_sha256=build_policy_sha256,
            lto_mode=lto_mode,
            **retention_fields,
            utc=now_utc,
            maintenance_required=True,
            publish_allowed=True,
            authoritative_state_read=True,
            state_generation=state.pointer.generation,
            state_publication_id=state.pointer.publication_id,
            reason="repository retention policy requires metadata convergence",
        )

    horizon = now.astimezone(timezone.utc) + timedelta(
        seconds=maintenance_horizon_seconds
    )
    if _valid_until(state) <= horizon:
        return DiscoveryDecision(
            decision="maintenance",
            source_version=source_version,
            source_dsc_sha256=source_dsc_sha256,
            dkc_revision=dkc_revision,
            build_policy_sha256=build_policy_sha256,
            lto_mode=lto_mode,
            **retention_fields,
            utc=now_utc,
            maintenance_required=True,
            publish_allowed=True,
            authoritative_state_read=True,
            state_generation=state.pointer.generation,
            state_publication_id=state.pointer.publication_id,
            reason="repository metadata is inside the refresh horizon",
        )
    return DiscoveryDecision(
        decision="no_op",
        source_version=source_version,
        source_dsc_sha256=source_dsc_sha256,
        dkc_revision=dkc_revision,
        build_policy_sha256=build_policy_sha256,
        lto_mode=lto_mode,
        **retention_fields,
        utc=now_utc,
        authoritative_state_read=True,
        state_generation=state.pointer.generation,
        state_publication_id=state.pointer.publication_id,
        reason="the newest source is already published and metadata is fresh",
    )
