"""The ORBIT Hydration seed graph — PRD §4.2, exactly 18 nodes.

The product is fictitious on purpose (§4: "to avoid third-party trademark and
claim issues"). Validations are attached records, not extra graph nodes.

The one design decision the PRD leaves implicit is where the legal copy line
lives, and it decides whether AS-01 holds. It is a project-revision *parameter*
bound only into `copy.pack`. Editing it therefore changes one node's operation and
invalidates exactly its descendants: copy.pack -> audio.narration, graphic.end_card,
compose.delivery_package. Four nodes, fourteen reused. Had the phrase been folded
into `source.brief`, `plan.shots` would also change, cascading through every
keyframe and clip and leaving nothing reusable.
See docs/decisions/0002-legal-line-is-a-bound-parameter.md.
"""

from __future__ import annotations

from takegraph_domain.enums import NodeType
from takegraph_domain.graph.types import GraphTemplate, InputSlot, ParameterBinding, TemplateNode

TEMPLATE_KEY = "orbit-launch"
TEMPLATE_VERSION = 1

PARAM_LEGAL_LINE = "legal_line"
PARAM_BRIEF_TEXT = "brief_text"

DELIVERY_KEY = "compose.delivery_package"
POSTER_KEY = "image.poster"
DELIVERABLE_KEYS = (DELIVERY_KEY, POSTER_KEY)

#: The four nodes a legal-copy edit must invalidate (§4.2, AS-01). Asserted by test,
#: never used to shortcut the impact algorithm — the algorithm has to derive it.
EXPECTED_LEGAL_COPY_REBUILD = (
    "copy.pack",
    "audio.narration",
    "graphic.end_card",
    "compose.delivery_package",
)


def _keyframe(index: int) -> TemplateNode:
    return TemplateNode(
        stable_key=f"image.keyframe.{index:02d}",
        node_type=NodeType.IMAGE_GENERATION,
        label=f"Keyframe {index}",
        inputs=(
            InputSlot(slot="product_reference", from_key="source.product_reference"),
            InputSlot(slot="cutout", from_key="transform.product_cutout"),
            InputSlot(slot="shot_plan", from_key="plan.shots", asset_role="plan"),
        ),
        operation={
            "prompt_template": (
                "Render shot {{ shot.index }} of the ORBIT Hydration launch using the "
                "approved product reference and cutout. Dark graphite set, crisp white "
                "bottle, teal orbital line, restrained orange accent."
            ),
            "shot_index": index,
            "parameters": {"aspect_ratio": "16:9", "quality": "high"},
        },
        provider_policy="orbit-image-v1",
        validation_policy="orbit-image-qc-v1",
        output_roles=("primary", "thumbnail"),
    )


def _clip(index: int) -> TemplateNode:
    return TemplateNode(
        stable_key=f"video.clip.{index:02d}",
        node_type=NodeType.VIDEO_GENERATION,
        label=f"Shot {index}",
        inputs=(
            InputSlot(slot="shot_plan", from_key="plan.shots", asset_role="plan"),
            InputSlot(slot="keyframe", from_key=f"image.keyframe.{index:02d}"),
        ),
        operation={
            "prompt_template": "Animate shot {{ shot.index }} using the approved keyframe.",
            "shot_index": index,
            "parameters": {"duration_seconds": 4, "aspect_ratio": "16:9"},
        },
        provider_policy="orbit-video-v1",
        validation_policy="orbit-video-qc-v1",
        output_roles=("primary", "thumbnail"),
    )


ORBIT_TEMPLATE = GraphTemplate(
    key=TEMPLATE_KEY,
    version=TEMPLATE_VERSION,
    nodes=(
        # 1
        TemplateNode(
            stable_key="source.brief",
            node_type=NodeType.SOURCE_TEXT,
            label="Creative brief",
            operation={"normalization": "nfc-collapse-whitespace"},
            parameter_bindings=(
                ParameterBinding(operation_key="brief_text", parameter=PARAM_BRIEF_TEXT),
            ),
        ),
        # 2
        TemplateNode(
            stable_key="source.product_reference",
            node_type=NodeType.SOURCE_IMAGE,
            label="Product reference",
            operation={"expected_media_kind": "image"},
        ),
        # 3
        TemplateNode(
            stable_key="transform.product_cutout",
            node_type=NodeType.IMAGE_TRANSFORM,
            label="Product cutout",
            inputs=(InputSlot(slot="source", from_key="source.product_reference"),),
            operation={"transform": "background_removal", "parameters": {"feather_px": 2}},
            validation_policy="orbit-image-qc-v1",
        ),
        # 4
        TemplateNode(
            stable_key="plan.shots",
            node_type=NodeType.STRUCTURED_PLAN,
            label="Four-shot plan",
            inputs=(
                InputSlot(slot="brief", from_key="source.brief"),
                InputSlot(slot="product_reference", from_key="source.product_reference"),
            ),
            operation={
                "prompt_template": "Produce a four-shot cinematic plan for the launch package.",
                "parameters": {"shot_count": 4},
                "output_schema": "shot_plan.v1",
            },
            provider_policy="orbit-text-v1",
            validation_policy="orbit-structured-qc-v1",
            output_roles=("plan",),
        ),
        # 5-8
        _keyframe(1),
        _keyframe(2),
        _keyframe(3),
        _keyframe(4),
        # 9-12
        _clip(1),
        _clip(2),
        _clip(3),
        _clip(4),
        # 13
        TemplateNode(
            stable_key="audio.music",
            node_type=NodeType.AUDIO_GENERATION,
            label="Music bed",
            inputs=(
                InputSlot(slot="brief", from_key="source.brief"),
                InputSlot(slot="shot_plan", from_key="plan.shots", asset_role="plan"),
            ),
            operation={
                "prompt_template": "Compose a restrained cinematic bed matching the brief tone.",
                "parameters": {"duration_seconds": 20},
            },
            provider_policy="orbit-audio-v1",
            validation_policy="orbit-audio-qc-v1",
        ),
        # 14 — the only node the legal line binds into.
        TemplateNode(
            stable_key="copy.pack",
            node_type=NodeType.STRUCTURED_TEXT,
            label="Copy pack",
            inputs=(InputSlot(slot="brief", from_key="source.brief"),),
            operation={
                "prompt_template": (
                    "Write narration, captions and the legal line for the launch package. "
                    "Use the required legal phrase verbatim."
                ),
                "output_schema": "copy_pack.v1",
            },
            parameter_bindings=(
                ParameterBinding(operation_key="required_legal_phrase", parameter=PARAM_LEGAL_LINE),
            ),
            provider_policy="orbit-text-v1",
            validation_policy="orbit-copy-qc-v1",
            output_roles=("copy",),
        ),
        # 15
        TemplateNode(
            stable_key="audio.narration",
            node_type=NodeType.AUDIO_GENERATION,
            label="Narration",
            inputs=(InputSlot(slot="copy", from_key="copy.pack", asset_role="copy"),),
            operation={"parameters": {"format": "wav", "sample_rate": 48000}},
            provider_policy="orbit-tts-v1",
            validation_policy="orbit-audio-qc-v1",
        ),
        # 16
        TemplateNode(
            stable_key="graphic.end_card",
            node_type=NodeType.IMAGE_COMPOSITION,
            label="End card",
            inputs=(
                InputSlot(slot="product_reference", from_key="source.product_reference"),
                InputSlot(slot="copy", from_key="copy.pack", asset_role="copy"),
            ),
            operation={"layout": "end_card.v1", "parameters": {"width": 1920, "height": 1080}},
            validation_policy="orbit-copy-qc-v1",
        ),
        # 17 — a deliverable, but not an input to the delivery package.
        TemplateNode(
            stable_key="image.poster",
            node_type=NodeType.IMAGE_COMPOSITION,
            label="Poster",
            inputs=(
                InputSlot(slot="product_reference", from_key="source.product_reference"),
                InputSlot(slot="keyframe", from_key="image.keyframe.01"),
            ),
            operation={"layout": "poster.v1", "parameters": {"width": 1080, "height": 1350}},
            validation_policy="orbit-image-qc-v1",
        ),
        # 18 — depends on nodes 9 through 16.
        TemplateNode(
            stable_key=DELIVERY_KEY,
            node_type=NodeType.MEDIA_COMPOSITION,
            label="Delivery package",
            inputs=(
                InputSlot(slot="clip", from_key="video.clip.01", ordinal=0),
                InputSlot(slot="clip", from_key="video.clip.02", ordinal=1),
                InputSlot(slot="clip", from_key="video.clip.03", ordinal=2),
                InputSlot(slot="clip", from_key="video.clip.04", ordinal=3),
                InputSlot(slot="music", from_key="audio.music"),
                InputSlot(slot="copy", from_key="copy.pack", asset_role="copy"),
                InputSlot(slot="narration", from_key="audio.narration"),
                InputSlot(slot="end_card", from_key="graphic.end_card"),
            ),
            operation={
                "outputs": [
                    "orbit-master-16x9.mp4",
                    "orbit-master-9x16.mp4",
                    "captions.vtt",
                    "final-audio.wav",
                ],
                "parameters": {
                    "reframe_policy": "center_crop_with_safe_area",
                    "end_card_seconds": 2,
                    "narration_duck_db": "-9.0",
                    "target_lufs": "-14.0",
                },
            },
            validation_policy="orbit-delivery-qc-v1",
            output_roles=("master_16x9", "master_9x16", "captions", "audio"),
        ),
    ),
)

#: Policy keys the template references. The compiler refuses to compile unless
#: every one resolves to an immutable version hash (§12.1 step 7).
REFERENCED_POLICIES = (
    "orbit-image-v1",
    "orbit-video-v1",
    "orbit-audio-v1",
    "orbit-text-v1",
    "orbit-tts-v1",
    "orbit-image-qc-v1",
    "orbit-video-qc-v1",
    "orbit-audio-qc-v1",
    "orbit-structured-qc-v1",
    "orbit-copy-qc-v1",
    "orbit-delivery-qc-v1",
)
