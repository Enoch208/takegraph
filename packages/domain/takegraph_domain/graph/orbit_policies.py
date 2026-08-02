"""Immutable ORBIT provider and validation policy definitions (PRD §9.2–9.3)."""

from __future__ import annotations

from takegraph_domain.canonical import JsonValue, canonical_hash, canonical_payload


def _provider_policy(
    key: str,
    *,
    provider: str,
    model: str,
    timeout_seconds: int,
    concurrency_key: str,
    max_concurrency: int,
    same_provider_fallback_models: list[str] | None = None,
    cross_provider_fallbacks: list[dict[str, JsonValue]] | None = None,
) -> dict[str, JsonValue]:
    model_fallbacks: list[JsonValue] = []
    model_fallbacks.extend(same_provider_fallback_models or [])
    provider_fallbacks: list[JsonValue] = []
    provider_fallbacks.extend(cross_provider_fallbacks or [])
    return {
        "schema_version": "1",
        "key": key,
        "version": 1,
        "primary": {
            "provider": provider,
            "model": model,
            "timeout_seconds": timeout_seconds,
            "same_provider_fallback_models": model_fallbacks,
        },
        "cross_provider_fallbacks": provider_fallbacks,
        "retry": {
            "max_transient_retries": 2,
            "base_delay_seconds": 2,
            "max_delay_seconds": 30,
            "jitter": True,
        },
        "budgets": {
            "max_total_attempts": 4,
            "max_elapsed_seconds": 900,
            "max_estimated_cost_usd": "5.000000",
        },
        "concurrency_key": concurrency_key,
        "max_concurrency": max_concurrency,
    }


ORBIT_PROVIDER_POLICIES: dict[str, dict[str, JsonValue]] = {
    "orbit-image-v1": _provider_policy(
        "orbit-image-v1",
        provider="gmicloud",
        model="${GMI_IMAGE_MODEL}",
        timeout_seconds=240,
        concurrency_key="image-generation",
        max_concurrency=4,
    ),
    "orbit-video-v1": _provider_policy(
        "orbit-video-v1",
        provider="gmicloud",
        model="${GMI_VIDEO_MODEL}",
        timeout_seconds=480,
        concurrency_key="video-generation",
        max_concurrency=2,
        same_provider_fallback_models=["${GMI_VIDEO_FALLBACK_MODEL}"],
        cross_provider_fallbacks=[
            {
                "provider": "runway",
                "model": "${RUNWAY_VIDEO_MODEL}",
                "timeout_seconds": 480,
                "required_credential": "RUNWAYML_API_SECRET",
            }
        ],
    ),
    "orbit-audio-v1": _provider_policy(
        "orbit-audio-v1",
        provider="elevenlabs",
        model="${ELEVENLABS_MUSIC_MODEL}",
        timeout_seconds=180,
        concurrency_key="music-generation",
        max_concurrency=2,
    ),
    "orbit-text-v1": _provider_policy(
        "orbit-text-v1",
        provider="anthropic",
        model="${EVALUATOR_MODEL}",
        timeout_seconds=120,
        concurrency_key="structured-text",
        max_concurrency=4,
    ),
    "orbit-tts-v1": _provider_policy(
        "orbit-tts-v1",
        provider="elevenlabs",
        model="${ELEVENLABS_TTS_MODEL}",
        timeout_seconds=180,
        concurrency_key="speech-generation",
        max_concurrency=2,
    ),
}


def _validation_policy(
    key: str,
    gates: list[tuple[str, str]],
    *,
    max_repair_iterations: int,
) -> dict[str, JsonValue]:
    return {
        "schema_version": "1",
        "key": key,
        "version": 1,
        "required_gates": [
            {"key": gate_key, "version": "1", "on_error": on_error} for gate_key, on_error in gates
        ],
        "decision": {
            "pass_threshold": "0.80",
            "review_threshold": "0.60",
            "max_repair_iterations": max_repair_iterations,
            "auto_repair_on": ["FAIL"] if max_repair_iterations else [],
            "human_review_on": ["REVIEW", "ERROR"],
        },
    }


ORBIT_VALIDATION_POLICIES: dict[str, dict[str, JsonValue]] = {
    "orbit-image-qc-v1": _validation_policy(
        "orbit-image-qc-v1",
        [
            ("storage_hash", "BLOCK"),
            ("media_integrity", "BLOCK"),
            ("dimensions", "BLOCK"),
            ("product_identity", "REVIEW"),
            ("manifest_integrity", "BLOCK"),
        ],
        max_repair_iterations=2,
    ),
    "orbit-video-qc-v1": _validation_policy(
        "orbit-video-qc-v1",
        [
            ("storage_hash", "BLOCK"),
            ("media_integrity", "BLOCK"),
            ("duration", "BLOCK"),
            ("product_identity", "REVIEW"),
            ("visual_continuity", "REVIEW"),
            ("manifest_integrity", "BLOCK"),
        ],
        max_repair_iterations=2,
    ),
    "orbit-audio-qc-v1": _validation_policy(
        "orbit-audio-qc-v1",
        [
            ("storage_hash", "BLOCK"),
            ("media_integrity", "BLOCK"),
            ("audio_properties", "BLOCK"),
            ("manifest_integrity", "BLOCK"),
        ],
        max_repair_iterations=2,
    ),
    "orbit-structured-qc-v1": _validation_policy(
        "orbit-structured-qc-v1",
        [("schema", "BLOCK"), ("required_fields", "BLOCK")],
        max_repair_iterations=1,
    ),
    "orbit-copy-qc-v1": _validation_policy(
        "orbit-copy-qc-v1",
        [
            ("required_phrase", "BLOCK"),
            ("superseded_phrase", "BLOCK"),
            ("schema", "BLOCK"),
        ],
        max_repair_iterations=1,
    ),
    "orbit-delivery-qc-v1": _validation_policy(
        "orbit-delivery-qc-v1",
        [
            ("storage_hash", "BLOCK"),
            ("media_integrity", "BLOCK"),
            ("delivery_dimensions", "BLOCK"),
            ("audio_properties", "BLOCK"),
            ("caption_integrity", "BLOCK"),
            ("manifest_integrity", "BLOCK"),
        ],
        max_repair_iterations=0,
    ),
}


def orbit_policy_hashes() -> dict[str, str]:
    policies = ORBIT_PROVIDER_POLICIES | ORBIT_VALIDATION_POLICIES
    for definition in policies.values():
        canonical_payload(definition)
    return {key: canonical_hash(definition) for key, definition in policies.items()}
