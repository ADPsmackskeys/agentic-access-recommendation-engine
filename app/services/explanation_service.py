"""Explainability (Phase 15 / Section 16).

Two artefacts are produced for every recommendation and both are persisted:

1. a **structured explanation** - the evidence, as data;
2. a **narrative** - the same evidence as prose.

The structured form is built deterministically from the decision record. The
narrative is either rendered from a template or written by an LLM, and the LLM
only ever sees the structured form. If the model fails, times out or is not
configured, the template narrative is used and the decision is untouched: an
explanation failure must never make a governance decision disappear.
"""

from __future__ import annotations

from app.config import Settings, get_settings
from app.domain.enums import ExplanationGenerator, RecommendationStatus
from app.domain.exceptions import LlmError
from app.domain.models import (
    AccessDecision,
    AnalysisExplanation,
    EmployeeProfile,
    PeerAnalysisResult,
    RecommendationExplanation,
    StructuredExplanation,
)
from app.logging import get_logger
from app.services.llm_service import LLMService, build_llm_service

logger = get_logger(__name__)

SYSTEM_PROMPT = """You are an access-governance analyst writing the audit note for an \
access recommendation that has ALREADY been decided by a deterministic policy engine.

Rules you must follow:
- Use ONLY facts present in the supplied JSON. Do not add, infer or estimate anything.
- Never contradict, re-open or re-rank the decision. `final_decision` and `approval_tier` \
are settled; your job is to explain them, not to evaluate them.
- Do not invent peer names, entitlement names, scores, policies or SoD rules.
- Write 2-4 short paragraphs of plain prose. No bullet lists, no headings, no markdown.
- Quote the numbers exactly as given (peer counts, affinity percentage, risk score).
"""


class ExplanationService:
    def __init__(
        self,
        settings: Settings | None = None,
        llm: LLMService | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.llm = llm if llm is not None else build_llm_service(self.settings)

    # ------------------------------------------------------------------ #
    # Structured half
    # ------------------------------------------------------------------ #
    def build_structured(
        self,
        decision: AccessDecision,
        peer_analysis: PeerAnalysisResult,
    ) -> StructuredExplanation:
        peer_evidence = [
            ev.evidence_value or f"{ev.peer_name} ({ev.peer_employee_id})"
            for ev in decision.evidence
        ]
        policy_results = (
            [
                f"{m.policy_id} ({m.policy_name}): {m.status.value} - {m.reason}"
                for m in decision.policy_result.matched_policies
            ]
            if decision.policy_result and decision.policy_result.matched_policies
            else ["All applicable policies passed."]
        )
        sod_results = (
            [
                f"{c.sod_id} ({c.name}): {c.severity.value} - {c.reason}"
                for c in decision.sod_conflicts
            ]
            if decision.sod_conflicts
            else ["No segregation-of-duties conflicts were detected."]
        )

        if decision.recommendation_status is RecommendationStatus.NOT_RECOMMENDED:
            why = (
                f"Only {decision.peer_count} of {decision.total_peers} matched peers hold this "
                f"entitlement, giving an affinity score of {decision.affinity_score}%, which is "
                f"below the {decision.affinity_threshold}% recommendation threshold."
            )
        else:
            why = (
                f"{decision.peer_count} of {decision.total_peers} matched "
                f"{peer_analysis.matching_strategy.value.replace('_', ' ')} peers hold this "
                f"entitlement, producing an affinity score of {decision.affinity_score}%."
            )

        return StructuredExplanation(
            recommendation=decision.entitlement_id,
            entitlement_name=decision.entitlement_name,
            application=decision.application,
            why_recommended=why,
            peer_evidence=peer_evidence,
            peer_summary=(
                f"{peer_analysis.peer_count} peer(s) matched using the "
                f"'{peer_analysis.matching_strategy.value}' strategy "
                f"(confidence {peer_analysis.confidence})."
            ),
            affinity=decision.affinity_score,
            risk=decision.risk_score,
            risk_level=decision.risk_level,
            policy_results=policy_results,
            sod_results=sod_results,
            final_decision=decision.recommendation_status,
            approval_tier=decision.approval_tier,
        )

    # ------------------------------------------------------------------ #
    # Narrative half
    # ------------------------------------------------------------------ #
    @staticmethod
    def render_template(structured: StructuredExplanation) -> str:
        """Deterministic narrative. Always available, never wrong."""
        risk_sentence = (
            f"The entitlement has a risk score of {structured.risk}, classified as "
            f"{structured.risk_level.value} risk."
        )

        if structured.final_decision is RecommendationStatus.NOT_RECOMMENDED:
            control_sentence = (
                "Risk, policy and segregation-of-duties controls were not evaluated because the "
                "entitlement did not pass the affinity threshold."
            )
        elif structured.sod_results and not structured.sod_results[0].startswith("No segregation"):
            control_sentence = "A segregation-of-duties conflict was detected: " + " ".join(
                structured.sod_results
            )
        elif structured.policy_results and structured.policy_results[0] != (
            "All applicable policies passed."
        ):
            control_sentence = "Policy evaluation returned: " + " ".join(
                structured.policy_results
            )
        else:
            control_sentence = (
                "All applicable policies passed and no SoD conflicts were detected."
            )

        return (
            f"{structured.recommendation} ({structured.entitlement_name}, "
            f"{structured.application}): {structured.why_recommended}\n\n"
            f"{risk_sentence} {control_sentence}\n\n"
            f"Final decision: {structured.final_decision.value}. "
            f"Approval tier: {structured.approval_tier.value}."
        )

    def generate_narrative(self, structured: StructuredExplanation) -> tuple[
        str, ExplanationGenerator, str | None, str | None
    ]:
        """Return (narrative, generator, model, error)."""
        template = self.render_template(structured)
        if not self.llm.available:
            return template, ExplanationGenerator.DETERMINISTIC, None, None

        try:
            narrative = self.llm.generate_narrative(
                system_prompt=SYSTEM_PROMPT,
                evidence=structured.model_dump(mode="json"),
            )
            return narrative, ExplanationGenerator.LLM, self.llm.model, None
        except LlmError as exc:
            logger.warning(
                "explanation.llm_failed_using_fallback",
                entitlement_id=structured.recommendation,
                error=exc.message,
            )
            return (
                template,
                ExplanationGenerator.DETERMINISTIC_FALLBACK,
                self.llm.model,
                exc.message,
            )
        except Exception as exc:  # defensive: never let prose break governance
            logger.warning(
                "explanation.llm_unexpected_error",
                entitlement_id=structured.recommendation,
                error=str(exc),
            )
            return (
                template,
                ExplanationGenerator.DETERMINISTIC_FALLBACK,
                self.llm.model,
                str(exc),
            )

    # ------------------------------------------------------------------ #
    # Whole analysis
    # ------------------------------------------------------------------ #
    def explain(
        self,
        *,
        employee: EmployeeProfile,
        peer_analysis: PeerAnalysisResult,
        decisions: list[AccessDecision],
    ) -> AnalysisExplanation:
        explanations: list[RecommendationExplanation] = []
        generators: set[ExplanationGenerator] = set()
        first_error: str | None = None

        for decision in decisions:
            structured = self.build_structured(decision, peer_analysis)
            narrative, generator, model, error = self.generate_narrative(structured)
            generators.add(generator)
            first_error = first_error or error
            explanations.append(
                RecommendationExplanation(
                    entitlement_id=decision.entitlement_id,
                    structured=structured,
                    narrative=narrative,
                    generator=generator,
                    model=model,
                    error=error,
                )
            )

        counts: dict[str, int] = {}
        for decision in decisions:
            counts[decision.recommendation_status.value] = (
                counts.get(decision.recommendation_status.value, 0) + 1
            )
        breakdown = ", ".join(f"{count} {status}" for status, count in sorted(counts.items()))

        summary = (
            f"{employee.name} ({employee.employee_id}), {employee.job_role} in "
            f"{employee.department} at level {employee.job_level}, was compared against "
            f"{peer_analysis.peer_count} peer(s) matched using the "
            f"'{peer_analysis.matching_strategy.value}' strategy. "
            f"{len(decisions)} candidate entitlement(s) were evaluated: "
            f"{breakdown or 'none'}."
        )

        generator = (
            ExplanationGenerator.LLM
            if generators == {ExplanationGenerator.LLM}
            else (
                ExplanationGenerator.DETERMINISTIC_FALLBACK
                if ExplanationGenerator.DETERMINISTIC_FALLBACK in generators
                else ExplanationGenerator.DETERMINISTIC
            )
        )
        logger.info(
            "explanation.generated",
            employee_id=employee.employee_id,
            count=len(explanations),
            generator=generator.value,
        )
        return AnalysisExplanation(
            employee_id=employee.employee_id,
            summary=summary,
            generator=generator,
            model=self.llm.model if self.llm.available else None,
            recommendations=explanations,
            error=first_error,
        )
