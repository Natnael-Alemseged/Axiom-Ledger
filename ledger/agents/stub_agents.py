"""
ledger/agents/stub_agents.py
============================
STUB IMPLEMENTATIONS for DocumentProcessingAgent, FraudDetectionAgent,
ComplianceAgent, and DecisionOrchestratorAgent.

Each stub contains:
  - The State TypedDict
  - build_graph() with the correct node sequence
  - All node method stubs with TODO instructions
  - The exact events each node must write
  - WHEN IT WORKS criteria for each agent

Pattern: follow CreditAnalysisAgent exactly. Same build_graph() structure,
same _record_node_execution() calls, same _append_with_retry() for domain writes.
"""
from __future__ import annotations
import logging
import time, json
from datetime import datetime
from decimal import Decimal
from typing import Any, NotRequired, TypedDict
from uuid import uuid4

from langgraph.graph import StateGraph, END

from ledger.agents.base_agent import BaseApexAgent
from ledger.integrations.document_refinery_adapter import extract_financial_facts
from ledger.schema.events import (
    CreditAnalysisRequested,
    DocumentFormatRejected,
    DocumentFormatValidated,
    DocumentType,
    ExtractionCompleted,
    ExtractionFailed,
    ExtractionStarted,
    FinancialFacts,
    PackageReadyForAnalysis,
    QualityAssessmentCompleted,
)

_LOG_DOC = logging.getLogger(__name__)


# ─── DOCUMENT PROCESSING AGENT ───────────────────────────────────────────────

class DocProcState(TypedDict):
    application_id: str
    session_id: str
    package_id: str | None
    document_ids: list[str] | None
    document_paths: list[str] | None
    documents_by_type: dict[str, dict[str, Any]] | None
    extraction_results: list[dict] | None  # one per document
    quality_assessment: dict | None
    quality_flags: list[str] | None
    errors: list[str]
    output_events: list[dict]
    next_agent: str | None
    next_agent_triggered: NotRequired[str | None]


class DocumentProcessingAgent(BaseApexAgent):
    """
    Wraps the Week 3 Document Intelligence pipeline.
    Processes uploaded PDFs and appends extraction events.

    LangGraph nodes:
        validate_inputs → validate_document_formats → extract_income_statement →
        extract_balance_sheet → assess_quality → write_output

    Output events:
        docpkg-{id}:  DocumentFormatValidated (x per doc), ExtractionStarted (x per doc),
                      ExtractionCompleted (x per doc), QualityAssessmentCompleted,
                      PackageReadyForAnalysis
        loan-{id}:    CreditAnalysisRequested

    WEEK 3 INTEGRATION:
        In _node_extract_document(), call your Week 3 pipeline:
            from document_refinery.pipeline import extract_financial_facts
            facts = await extract_financial_facts(file_path, document_type)
        Wrap in try/except — append ExtractionFailed if pipeline raises.

    LLM in _node_assess_quality():
        System: "You are a financial document quality analyst.
                 Check internal consistency. Do NOT make credit decisions.
                 Return DocumentQualityAssessment JSON."
        The LLM checks: Assets = Liabilities + Equity, margins plausible, etc.

    WHEN THIS WORKS:
        pytest tests/phase2/test_document_agent.py  # all pass
        python scripts/run_pipeline.py --app APEX-0001 --phase document
          → ExtractionCompleted event in docpkg stream with non-null total_revenue
          → QualityAssessmentCompleted event present
          → PackageReadyForAnalysis event present
          → CreditAnalysisRequested on loan stream
    """

    QUALITY_SYSTEM_PROMPT = """
You are a financial document quality analyst. You receive structured data
extracted from a company's financial statements.

Check ONLY:
1. Internal consistency (Gross Profit = Revenue - COGS, Assets = Liabilities + Equity)
2. Implausible values (margins > 80%, negative equity without note)
3. Critical missing fields (total_revenue, net_income, total_assets, total_liabilities)

Return JSON: {"overall_confidence": float, "is_coherent": bool,
  "anomalies": [str], "critical_missing_fields": [str],
  "reextraction_recommended": bool, "auditor_notes": str}

DO NOT make credit or lending decisions. DO NOT suggest loan outcomes.
"""

    def build_graph(self):
        g = StateGraph(DocProcState)
        g.add_node("validate_inputs",            self._node_validate_inputs)
        g.add_node("validate_document_formats",  self._node_validate_formats)
        g.add_node("extract_income_statement",   self._node_extract_is)
        g.add_node("extract_balance_sheet",      self._node_extract_bs)
        g.add_node("assess_quality",             self._node_assess_quality)
        g.add_node("write_output",               self._node_write_output)

        g.set_entry_point("validate_inputs")
        g.add_edge("validate_inputs",           "validate_document_formats")
        g.add_edge("validate_document_formats", "extract_income_statement")
        g.add_edge("extract_income_statement",  "extract_balance_sheet")
        g.add_edge("extract_balance_sheet",     "assess_quality")
        g.add_edge("assess_quality",            "write_output")
        g.add_edge("write_output",              END)
        return g.compile()

    def _initial_state(self, application_id: str) -> DocProcState:
        return DocProcState(
            application_id=application_id, session_id=self.session_id,
            package_id=None,
            document_ids=None, document_paths=None,
            documents_by_type=None,
            extraction_results=None, quality_assessment=None,
            quality_flags=None,
            errors=[], output_events=[], next_agent=None,
        )

    @staticmethod
    def _safe_json_extract(text: str) -> dict[str, Any]:
        raw = (text or "").strip()
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            pass
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            try:
                parsed = json.loads(raw[start : end + 1])
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                return {}
        return {}

    @staticmethod
    def _merge_extracted_facts(extraction_results: list[dict]) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        merged_confidence: dict[str, float] = {}
        merged_notes: list[str] = []
        for result in extraction_results:
            facts = result.get("facts") or {}
            for k, v in facts.items():
                if k in {"field_confidence", "extraction_notes"}:
                    continue
                if v is not None and merged.get(k) is None:
                    merged[k] = v
            for field, confidence in (facts.get("field_confidence") or {}).items():
                if field not in merged_confidence:
                    merged_confidence[field] = confidence
                else:
                    merged_confidence[field] = min(merged_confidence[field], confidence)
            merged_notes.extend(facts.get("extraction_notes") or [])
        merged["field_confidence"] = merged_confidence
        merged["extraction_notes"] = merged_notes
        return merged

    async def _node_validate_inputs(self, state: DocProcState) -> DocProcState:
        t = time.time()
        app_id = state["application_id"]
        _LOG_DOC.info("docproc_validate_inputs_start application_id=%s", app_id)
        loan_events = await self.store.load_stream(f"loan-{app_id}")
        pkg_events = await self.store.load_stream(f"docpkg-{app_id}")
        package_created = next((e for e in reversed(pkg_events) if e["event_type"] == "PackageCreated"), None)
        package_id = ((package_created or {}).get("payload") or {}).get("package_id", app_id)

        uploaded = [e for e in loan_events if e["event_type"] == "DocumentUploaded"]
        required = {"application_proposal", "income_statement", "balance_sheet"}
        selected: dict[str, dict[str, Any]] = {}
        for ev in reversed(uploaded):
            payload = ev.get("payload", {})
            dtype = payload.get("document_type")
            if dtype in required and dtype not in selected:
                selected[dtype] = payload
                if len(selected) == len(required):
                    break

        missing = sorted(required - set(selected.keys()))
        if missing:
            errors = [f"Missing required uploaded documents: {', '.join(missing)}"]
            _LOG_DOC.warning("docproc_validate_inputs_missing application_id=%s missing=%s", app_id, missing)
            await self._record_input_failed(missing, errors)
            raise ValueError("; ".join(errors))

        ms = int((time.time() - t) * 1000)
        _LOG_DOC.info(
            "docproc_validate_inputs_ok application_id=%s package_id=%s doc_types=%s duration_ms=%s",
            app_id,
            package_id,
            sorted(selected.keys()),
            ms,
        )
        await self._record_input_validated(["application_id", "document_ids", "file_paths", "package_id"], ms)
        await self._record_node_execution(
            "validate_inputs",
            ["application_id"],
            ["document_ids", "document_paths", "documents_by_type", "package_id"],
            ms,
        )
        return {
            **state,
            "package_id": package_id,
            "document_ids": [selected[k]["document_id"] for k in sorted(selected.keys())],
            "document_paths": [selected[k]["file_path"] for k in sorted(selected.keys())],
            "documents_by_type": selected,
        }

    async def _node_validate_formats(self, state: DocProcState) -> DocProcState:
        t = time.time()
        app_id = state["application_id"]
        _LOG_DOC.info("docproc_validate_formats_start application_id=%s", app_id)
        package_id = state.get("package_id") or app_id
        docs = state.get("documents_by_type") or {}
        errors = list(state.get("errors") or [])
        valid_docs: dict[str, dict[str, Any]] = {}

        import pathlib
        import pdfplumber

        for dtype, payload in docs.items():
            doc_id = payload["document_id"]
            path = pathlib.Path(payload["file_path"])
            _LOG_DOC.debug("docproc_validate_pdf doc_id=%s type=%s path=%s", doc_id, dtype, path)
            try:
                if not path.exists():
                    raise FileNotFoundError(str(path))
                if path.suffix.lower() != ".pdf":
                    raise ValueError(f"Unsupported format for Week 3 extraction: {path.suffix}")
                with pdfplumber.open(path) as pdf:
                    page_count = len(pdf.pages)
                ev = DocumentFormatValidated(
                    package_id=package_id,
                    document_id=doc_id,
                    document_type=DocumentType(dtype),
                    page_count=page_count,
                    detected_format="pdf",
                    validated_at=datetime.now(),
                ).to_store_dict()
                await self._append_stream(f"docpkg-{app_id}", ev)
                valid_docs[dtype] = payload
            except Exception as exc:
                errors.append(f"{doc_id}: {exc}")
                rej = DocumentFormatRejected(
                    package_id=package_id,
                    document_id=doc_id,
                    rejection_reason=str(exc)[:500],
                    rejected_at=datetime.now(),
                ).to_store_dict()
                await self._append_stream(f"docpkg-{app_id}", rej)

        if "income_statement" not in valid_docs or "balance_sheet" not in valid_docs:
            _LOG_DOC.error("docproc_validate_formats_failed application_id=%s errors=%s", app_id, errors)
            raise ValueError("Required PDFs for income_statement and balance_sheet are not valid.")

        ms = int((time.time() - t) * 1000)
        _LOG_DOC.info("docproc_validate_formats_ok application_id=%s valid_types=%s duration_ms=%s", app_id, list(valid_docs.keys()), ms)
        await self._record_node_execution(
            "validate_document_formats",
            ["documents_by_type"],
            ["documents_by_type"],
            ms,
        )
        return {**state, "documents_by_type": valid_docs, "errors": errors}

    async def _extract_document(self, state: DocProcState, document_type: str, node_name: str) -> DocProcState:
        t = time.time()
        app_id = state["application_id"]
        _LOG_DOC.info("docproc_extract_start application_id=%s node=%s document_type=%s", app_id, node_name, document_type)
        package_id = state.get("package_id") or app_id
        docs = state.get("documents_by_type") or {}
        doc = docs.get(document_type)
        if not doc:
            raise ValueError(f"Missing document payload for {document_type}")

        doc_id = doc["document_id"]
        file_path = doc["file_path"]
        started = ExtractionStarted(
            package_id=package_id,
            document_id=doc_id,
            document_type=DocumentType(document_type),
            pipeline_version="week3-refinery",
            extraction_model="refinery.run_extraction",
            started_at=datetime.now(),
        ).to_store_dict()
        await self._append_stream(f"docpkg-{app_id}", started, causation_id=self.session_id)

        extraction_results = list(state.get("extraction_results") or [])
        errors = list(state.get("errors") or [])
        try:
            adapter_result = await extract_financial_facts(file_path, document_type)
            facts_dict = adapter_result["facts"]
            completed = ExtractionCompleted(
                package_id=package_id,
                document_id=doc_id,
                document_type=DocumentType(document_type),
                facts=FinancialFacts(**facts_dict),
                raw_text_length=max(0, int(adapter_result["raw_text_length"])),
                tables_extracted=max(0, int(adapter_result["tables_extracted"])),
                processing_ms=max(1, int(adapter_result["processing_ms"])),
                completed_at=datetime.now(),
            ).to_store_dict()
            await self._append_stream(f"docpkg-{app_id}", completed, causation_id=self.session_id)
            extraction_results.append(
                {"document_id": doc_id, "document_type": document_type, "facts": facts_dict}
            )
            await self._record_tool_call(
                "week3_extraction_pipeline",
                f"path={file_path},type={document_type}",
                f"status={adapter_result['status']} strategy={adapter_result['strategy_used']}",
                int((time.time() - t) * 1000),
            )
            _LOG_DOC.info(
                "docproc_extract_ok application_id=%s doc_id=%s type=%s strategy=%s status=%s processing_ms=%s",
                app_id,
                doc_id,
                document_type,
                adapter_result["strategy_used"],
                adapter_result["status"],
                adapter_result["processing_ms"],
            )
        except Exception as exc:
            _LOG_DOC.exception(
                "docproc_extract_failed application_id=%s doc_id=%s type=%s path=%s",
                app_id,
                doc_id,
                document_type,
                file_path,
            )
            errors.append(f"{document_type} extraction failed: {exc}")
            failed = ExtractionFailed(
                package_id=package_id,
                document_id=doc_id,
                error_type=type(exc).__name__,
                error_message=str(exc)[:500],
                partial_facts=None,
                failed_at=datetime.now(),
            ).to_store_dict()
            await self._append_stream(f"docpkg-{app_id}", failed, causation_id=self.session_id)
            await self._record_tool_call(
                "week3_extraction_pipeline",
                f"path={file_path},type={document_type}",
                f"failed={type(exc).__name__}",
                int((time.time() - t) * 1000),
            )

        ms = int((time.time() - t) * 1000)
        await self._record_node_execution(
            node_name,
            ["documents_by_type"],
            ["extraction_results"],
            ms,
        )
        return {**state, "extraction_results": extraction_results, "errors": errors}

    async def _node_extract_is(self, state: DocProcState) -> DocProcState:
        return await self._extract_document(state, "income_statement", "extract_income_statement")

    async def _node_extract_bs(self, state: DocProcState) -> DocProcState:
        return await self._extract_document(state, "balance_sheet", "extract_balance_sheet")

    async def _node_assess_quality(self, state: DocProcState) -> DocProcState:
        t = time.time()
        app_id = state["application_id"]
        _LOG_DOC.info("docproc_assess_quality_start application_id=%s extraction_docs=%s", app_id, len(state.get("extraction_results") or []))
        package_id = state.get("package_id") or app_id
        extraction_results = list(state.get("extraction_results") or [])
        merged_facts = self._merge_extracted_facts(extraction_results)
        critical = ["total_revenue", "net_income", "total_assets", "total_liabilities"]
        critical_missing = [k for k in critical if merged_facts.get(k) is None]

        ti: int | None = None
        to: int | None = None
        cost: float | None = None
        fallback_notes = []
        content = ""
        try:
            user_prompt = (
                "Assess the following extracted financial facts for coherence.\n"
                f"Facts JSON:\n{json.dumps(merged_facts, default=str, indent=2)}"
            )
            content, ti, to, cost = await self._call_llm(
                self.QUALITY_SYSTEM_PROMPT,
                user_prompt,
                max_tokens=512,
            )
            qa = self._safe_json_extract(content)
        except Exception as exc:
            qa = {}
            fallback_notes.append(f"LLM quality assessment fallback used: {type(exc).__name__}: {exc!s:.200}")
            ti = to = cost = None

        if fallback_notes:
            _LOG_DOC.warning(
                "docproc_assess_quality_llm_fallback application_id=%s — %s "
                "(confidence/metrics below may be defaults, not model output)",
                app_id,
                "; ".join(fallback_notes),
            )
        else:
            _LOG_DOC.info(
                "docproc_assess_quality_llm_ok application_id=%s prompt_tokens=%s output_tokens=%s",
                app_id,
                ti,
                to,
            )

        anomalies: list[str] = list(qa.get("anomalies") or [])
        revenue = merged_facts.get("total_revenue")
        gross_profit = merged_facts.get("gross_profit")
        if isinstance(revenue, Decimal) and isinstance(gross_profit, Decimal):
            if revenue != 0 and float(gross_profit / revenue) > 0.80:
                anomalies.append("Gross margin exceeds 80% and may be implausible.")
        assets = merged_facts.get("total_assets")
        liabilities = merged_facts.get("total_liabilities")
        equity = merged_facts.get("total_equity")
        if isinstance(assets, Decimal) and isinstance(liabilities, Decimal) and isinstance(equity, Decimal):
            delta = assets - liabilities - equity
            if abs(delta) > Decimal("1.00"):
                anomalies.append("Balance sheet inconsistency: assets != liabilities + equity.")
        if isinstance(equity, Decimal) and equity < 0:
            anomalies.append("Negative equity detected without explicit note.")

        qa_result = {
            "overall_confidence": float(qa.get("overall_confidence", 0.55 if critical_missing else 0.78)),
            "is_coherent": bool(qa.get("is_coherent", len(anomalies) == 0 and not critical_missing)),
            "anomalies": anomalies,
            "critical_missing_fields": list(qa.get("critical_missing_fields") or critical_missing),
            "reextraction_recommended": bool(
                qa.get("reextraction_recommended", bool(critical_missing) or len(anomalies) > 0)
            ),
            "auditor_notes": str(
                qa.get(
                    "auditor_notes",
                    " ; ".join(fallback_notes) if fallback_notes else "Automated coherence checks completed.",
                )
            ),
        }

        doc_id = (extraction_results[0] if extraction_results else {}).get("document_id", f"docpkg-{app_id}")
        event = QualityAssessmentCompleted(
            package_id=package_id,
            document_id=doc_id,
            overall_confidence=qa_result["overall_confidence"],
            is_coherent=qa_result["is_coherent"],
            anomalies=qa_result["anomalies"],
            critical_missing_fields=qa_result["critical_missing_fields"],
            reextraction_recommended=qa_result["reextraction_recommended"],
            auditor_notes=qa_result["auditor_notes"],
            assessed_at=datetime.now(),
        ).to_store_dict()
        await self._append_stream(f"docpkg-{app_id}", event, causation_id=self.session_id)

        ms = int((time.time() - t) * 1000)
        _LOG_DOC.info(
            "docproc_assess_quality_done application_id=%s coherent=%s confidence=%.3f reextract=%s anomalies=%s",
            app_id,
            qa_result["is_coherent"],
            qa_result["overall_confidence"],
            qa_result["reextraction_recommended"],
            len(qa_result["anomalies"]),
        )
        await self._record_node_execution(
            "assess_quality",
            ["extraction_results"],
            ["quality_assessment", "quality_flags"],
            ms,
            ti,
            to,
            cost,
        )
        return {
            **state,
            "quality_assessment": qa_result,
            "quality_flags": qa_result["critical_missing_fields"] + qa_result["anomalies"],
        }

    async def _node_write_output(self, state: DocProcState) -> DocProcState:
        t = time.time()
        app_id = state["application_id"]
        _LOG_DOC.info("docproc_write_output_start application_id=%s", app_id)
        package_id = state.get("package_id") or app_id
        extraction_results = list(state.get("extraction_results") or [])
        qa = state.get("quality_assessment") or {}
        quality_flags = list(state.get("quality_flags") or [])

        ready = PackageReadyForAnalysis(
            package_id=package_id,
            application_id=app_id,
            documents_processed=len(extraction_results),
            has_quality_flags=bool(quality_flags),
            quality_flag_count=len(quality_flags),
            ready_at=datetime.now(),
        ).to_store_dict()
        await self._append_stream(f"docpkg-{app_id}", ready, causation_id=self.session_id)

        credit_requested = CreditAnalysisRequested(
            application_id=app_id,
            requested_at=datetime.now(),
            requested_by="DocumentProcessingAgent",
            priority="HIGH" if qa.get("reextraction_recommended") else "NORMAL",
        ).to_store_dict()
        await self._append_stream(f"loan-{app_id}", credit_requested, causation_id=self.session_id)

        events_written = [
            {"stream_id": f"docpkg-{app_id}", "event_type": "PackageReadyForAnalysis", "stream_position": -1},
            {"stream_id": f"loan-{app_id}", "event_type": "CreditAnalysisRequested", "stream_position": -1},
        ]
        summary = (
            f"Processed {len(extraction_results)} required financial docs; "
            f"quality_flags={len(quality_flags)}; credit analysis requested."
        )
        await self._record_output_written(events_written, summary)
        ms = int((time.time() - t) * 1000)
        await self._record_node_execution("write_output", ["quality_assessment"], ["events_written"], ms)
        _LOG_DOC.info(
            "docproc_write_output_done application_id=%s next_agent=credit_analysis docs_processed=%s",
            app_id,
            len(extraction_results),
        )
        return {
            **state,
            "output_events": events_written,
            "next_agent": "credit_analysis",
            "next_agent_triggered": "credit_analysis",
        }


# ─── FRAUD DETECTION AGENT ───────────────────────────────────────────────────

class FraudState(TypedDict):
    application_id: str
    session_id: str
    extracted_facts: dict | None
    registry_profile: dict | None
    historical_financials: list[dict] | None
    fraud_signals: list[dict] | None
    fraud_score: float | None
    anomalies: list[dict] | None
    errors: list[str]
    output_events: list[dict]
    next_agent: str | None


class FraudDetectionAgent(BaseApexAgent):
    """
    Cross-references extracted document facts against historical registry data.
    Detects anomalous discrepancies that suggest fraud or document manipulation.

    LangGraph nodes:
        validate_inputs → load_document_facts → cross_reference_registry →
        analyze_fraud_patterns → write_output

    Output events:
        fraud-{id}: FraudScreeningInitiated, FraudAnomalyDetected (0..N),
                    FraudScreeningCompleted
        loan-{id}:  ComplianceCheckRequested

    KEY SCORING LOGIC:
        fraud_score = base(0.05)
            + revenue_discrepancy_factor   (doc revenue vs prior year registry)
            + submission_pattern_factor    (channel, timing, IP region)
            + balance_sheet_consistency    (assets = liabilities + equity within tolerance)

        revenue_discrepancy_factor:
            gap = abs(doc_revenue - registry_prior_revenue) / registry_prior_revenue
            if gap > 0.40 and trajectory not in (GROWTH, RECOVERING): += 0.25

        FraudAnomalyDetected is appended for each anomaly where severity >= MEDIUM.
        fraud_score > 0.60 → recommendation = "DECLINE"
        fraud_score 0.30..0.60 → "FLAG_FOR_REVIEW"
        fraud_score < 0.30 → "PROCEED"

    LLM in _node_analyze():
        System: "You are a financial fraud analyst.
                 Given the cross-reference results, identify specific named anomalies.
                 For each anomaly: type, severity, evidence, affected_fields.
                 Compute a final fraud_score 0-1. Return FraudAssessment JSON."

    WHEN THIS WORKS:
        pytest tests/phase2/test_fraud_agent.py
          → FraudScreeningCompleted event in fraud stream
          → fraud_score between 0.0 and 1.0
          → ComplianceCheckRequested on loan stream
          → NARR-03 (crash recovery) test passes
    """

    def build_graph(self):
        g = StateGraph(FraudState)
        g.add_node("validate_inputs",         self._node_validate_inputs)
        g.add_node("load_document_facts",     self._node_load_facts)
        g.add_node("cross_reference_registry",self._node_cross_reference)
        g.add_node("analyze_fraud_patterns",  self._node_analyze)
        g.add_node("write_output",            self._node_write_output)

        g.set_entry_point("validate_inputs")
        g.add_edge("validate_inputs",          "load_document_facts")
        g.add_edge("load_document_facts",      "cross_reference_registry")
        g.add_edge("cross_reference_registry", "analyze_fraud_patterns")
        g.add_edge("analyze_fraud_patterns",   "write_output")
        g.add_edge("write_output",             END)
        return g.compile()

    def _initial_state(self, application_id: str) -> FraudState:
        return FraudState(
            application_id=application_id, session_id=self.session_id,
            extracted_facts=None, registry_profile=None, historical_financials=None,
            fraud_signals=None, fraud_score=None, anomalies=None,
            errors=[], output_events=[], next_agent=None,
        )

    async def _node_validate_inputs(self, state): raise NotImplementedError
    async def _node_load_facts(self, state):      raise NotImplementedError
    async def _node_cross_reference(self, state): raise NotImplementedError
    async def _node_analyze(self, state):         raise NotImplementedError
    async def _node_write_output(self, state):    raise NotImplementedError


# ─── COMPLIANCE AGENT ─────────────────────────────────────────────────────────

class ComplianceState(TypedDict):
    application_id: str
    session_id: str
    company_profile: dict | None
    rule_results: list[dict] | None
    has_hard_block: bool
    block_rule_id: str | None
    errors: list[str]
    output_events: list[dict]
    next_agent: str | None


# Regulation definitions — deterministic, no LLM in decision path
REGULATIONS = {
    "REG-001": {
        "name": "Bank Secrecy Act (BSA) Check",
        "version": "2026-Q1-v1",
        "is_hard_block": False,
        "check": lambda co: not any(
            f.get("flag_type") == "AML_WATCH" and f.get("is_active")
            for f in co.get("compliance_flags", [])
        ),
        "failure_reason": "Active AML Watch flag present. Remediation required.",
        "remediation": "Provide enhanced due diligence documentation within 10 business days.",
    },
    "REG-002": {
        "name": "OFAC Sanctions Screening",
        "version": "2026-Q1-v1",
        "is_hard_block": True,
        "check": lambda co: not any(
            f.get("flag_type") == "SANCTIONS_REVIEW" and f.get("is_active")
            for f in co.get("compliance_flags", [])
        ),
        "failure_reason": "Active OFAC Sanctions Review. Application blocked.",
        "remediation": None,
    },
    "REG-003": {
        "name": "Jurisdiction Lending Eligibility",
        "version": "2026-Q1-v1",
        "is_hard_block": True,
        "check": lambda co: co.get("jurisdiction") != "MT",
        "failure_reason": "Jurisdiction MT not approved for commercial lending at this time.",
        "remediation": None,
    },
    "REG-004": {
        "name": "Legal Entity Type Eligibility",
        "version": "2026-Q1-v1",
        "is_hard_block": False,
        "check": lambda co: not (
            co.get("legal_type") == "Sole Proprietor"
            and (co.get("requested_amount_usd", 0) or 0) > 250_000
        ),
        "failure_reason": "Sole Proprietor loans >$250K require additional documentation.",
        "remediation": "Submit SBA Form 912 and personal financial statement.",
    },
    "REG-005": {
        "name": "Minimum Operating History",
        "version": "2026-Q1-v1",
        "is_hard_block": True,
        "check": lambda co: (2024 - (co.get("founded_year") or 2024)) >= 2,
        "failure_reason": "Business must have at least 2 years of operating history.",
        "remediation": None,
    },
    "REG-006": {
        "name": "CRA Community Reinvestment",
        "version": "2026-Q1-v1",
        "is_hard_block": False,
        "check": lambda co: True,   # Always noted, never fails
        "note_type": "CRA_CONSIDERATION",
        "note_text": "Jurisdiction qualifies for Community Reinvestment Act consideration.",
    },
}


class ComplianceAgent(BaseApexAgent):
    """
    Evaluates 6 deterministic regulatory rules in sequence.
    Stops at first hard block (is_hard_block=True).
    LLM not used in rule evaluation — only for human-readable evidence summaries.

    LangGraph nodes:
        validate_inputs → load_company_profile → evaluate_reg001 → evaluate_reg002 →
        evaluate_reg003 → evaluate_reg004 → evaluate_reg005 → evaluate_reg006 → write_output

    Note: Use conditional edges after each rule so hard blocks skip remaining rules.
    See add_conditional_edges() in LangGraph docs.

    Output events:
        compliance-{id}: ComplianceCheckInitiated,
                         ComplianceRulePassed/Failed/Noted (one per rule evaluated),
                         ComplianceCheckCompleted
        loan-{id}:       DecisionRequested (if no hard block)
                         ApplicationDeclined (if hard block)

    RULE EVALUATION PATTERN (each _node_evaluate_regXXX):
        1. co = state["company_profile"]
        2. passes = REGULATIONS[rule_id]["check"](co)
        3. eh = self._sha(f"{rule_id}-{co['company_id']}")
        4. If passes: append ComplianceRulePassed or ComplianceRuleNoted
        5. If fails: append ComplianceRuleFailed; if is_hard_block: set state["has_hard_block"]=True
        6. await self._record_node_execution(...)

    ROUTING:
        After each rule node, use conditional edge:
            g.add_conditional_edges(
                "evaluate_reg001",
                lambda s: "write_output" if s["has_hard_block"] else "evaluate_reg002",
            )

    WHEN THIS WORKS:
        pytest tests/phase2/test_compliance_agent.py
          → ComplianceCheckCompleted with correct verdict
          → NARR-04 (Montana REG-003 hard block): no DecisionRequested event,
            ApplicationDeclined present, adverse_action_notice_required=True
    """

    def build_graph(self):
        g = StateGraph(ComplianceState)
        g.add_node("validate_inputs",     self._node_validate_inputs)
        g.add_node("load_company_profile",self._node_load_profile)
        g.add_node("evaluate_reg001",     lambda s: self._evaluate_rule(s, "REG-001"))
        g.add_node("evaluate_reg002",     lambda s: self._evaluate_rule(s, "REG-002"))
        g.add_node("evaluate_reg003",     lambda s: self._evaluate_rule(s, "REG-003"))
        g.add_node("evaluate_reg004",     lambda s: self._evaluate_rule(s, "REG-004"))
        g.add_node("evaluate_reg005",     lambda s: self._evaluate_rule(s, "REG-005"))
        g.add_node("evaluate_reg006",     lambda s: self._evaluate_rule(s, "REG-006"))
        g.add_node("write_output",        self._node_write_output)

        g.set_entry_point("validate_inputs")
        g.add_edge("validate_inputs",      "load_company_profile")
        g.add_edge("load_company_profile", "evaluate_reg001")

        # Conditional edges: stop at hard block, proceed otherwise
        for src, nxt in [
            ("evaluate_reg001", "evaluate_reg002"),
            ("evaluate_reg002", "evaluate_reg003"),
            ("evaluate_reg003", "evaluate_reg004"),
            ("evaluate_reg004", "evaluate_reg005"),
            ("evaluate_reg005", "evaluate_reg006"),
            ("evaluate_reg006", "write_output"),
        ]:
            g.add_conditional_edges(
                src,
                lambda s, _nxt=nxt: "write_output" if s["has_hard_block"] else _nxt,
            )
        g.add_edge("write_output", END)
        return g.compile()

    def _initial_state(self, application_id: str) -> ComplianceState:
        return ComplianceState(
            application_id=application_id, session_id=self.session_id,
            company_profile=None, rule_results=[], has_hard_block=False,
            block_rule_id=None, errors=[], output_events=[], next_agent=None,
        )

    async def _node_validate_inputs(self, state): raise NotImplementedError
    async def _node_load_profile(self, state):    raise NotImplementedError

    async def _evaluate_rule(self, state: ComplianceState, rule_id: str) -> ComplianceState:
        """
        TODO:
        1. reg = REGULATIONS[rule_id]
        2. co = state["company_profile"] — add "requested_amount_usd" from app
        3. passes = reg["check"](co)
        4. evidence_hash = self._sha(f"{rule_id}-{co['company_id']}-{passes}")
        5. If REG-006 (always noted):
               append ComplianceRuleNoted to "compliance-{app_id}" stream
        6. Elif passes:
               append ComplianceRulePassed
        7. Else:
               append ComplianceRuleFailed
               if reg["is_hard_block"]: state["has_hard_block"]=True, state["block_rule_id"]=rule_id
        8. await self._record_node_execution(f"evaluate_{rule_id.lower().replace('-','_')}", ...)
        """
        raise NotImplementedError(f"Implement _evaluate_rule for {rule_id}")

    async def _node_write_output(self, state): raise NotImplementedError


# ─── DECISION ORCHESTRATOR ────────────────────────────────────────────────────

class OrchestratorState(TypedDict):
    application_id: str
    session_id: str
    credit_result: dict | None
    fraud_result: dict | None
    compliance_result: dict | None
    recommendation: str | None
    confidence: float | None
    approved_amount: float | None
    executive_summary: str | None
    conditions: list[str] | None
    hard_constraints_applied: list[str] | None
    errors: list[str]
    output_events: list[dict]
    next_agent: str | None


class DecisionOrchestratorAgent(BaseApexAgent):
    """
    Synthesises all prior agent outputs into a final recommendation.
    The only agent that reads from multiple aggregate streams before deciding.

    LangGraph nodes:
        validate_inputs → load_credit_result → load_fraud_result →
        load_compliance_result → synthesize_decision → apply_hard_constraints →
        write_output

    Input streams read (load_* nodes):
        credit-{id}:     CreditAnalysisCompleted (last event of this type)
        fraud-{id}:      FraudScreeningCompleted
        compliance-{id}: ComplianceCheckCompleted

    Output events:
        loan-{id}:  DecisionGenerated
                    ApplicationApproved (if APPROVE)
                    ApplicationDeclined (if DECLINE)
                    HumanReviewRequested (if REFER)

    HARD CONSTRAINTS (Python, not LLM — applied in apply_hard_constraints node):
        1. compliance BLOCKED → recommendation = DECLINE (cannot override)
        2. confidence < 0.60 → recommendation = REFER
        3. fraud_score > 0.60 → recommendation = REFER
        4. risk_tier == HIGH and confidence < 0.70 → recommendation = REFER

    LLM in synthesize_decision:
        System: "You are a senior loan officer synthesising multi-agent analysis.
                 Produce a recommendation (APPROVE/DECLINE/REFER),
                 approved_amount_usd, executive_summary (3-5 sentences),
                 and key_risks list. Return OrchestratorDecision JSON."
        NOTE: The LLM recommendation may be overridden by apply_hard_constraints.
              Log this override in DecisionGenerated.policy_overrides_applied.

    WHEN THIS WORKS:
        pytest tests/phase2/test_orchestrator_agent.py
          → DecisionGenerated event on loan stream
          → NARR-05 (human override): DecisionGenerated.recommendation="DECLINE",
            followed by HumanReviewCompleted.override=True,
            followed by ApplicationApproved with correct override fields
    """

    def build_graph(self):
        g = StateGraph(OrchestratorState)
        g.add_node("validate_inputs",         self._node_validate_inputs)
        g.add_node("load_credit_result",      self._node_load_credit)
        g.add_node("load_fraud_result",       self._node_load_fraud)
        g.add_node("load_compliance_result",  self._node_load_compliance)
        g.add_node("synthesize_decision",     self._node_synthesize)
        g.add_node("apply_hard_constraints",  self._node_constraints)
        g.add_node("write_output",            self._node_write_output)

        g.set_entry_point("validate_inputs")
        g.add_edge("validate_inputs",        "load_credit_result")
        g.add_edge("load_credit_result",     "load_fraud_result")
        g.add_edge("load_fraud_result",      "load_compliance_result")
        g.add_edge("load_compliance_result", "synthesize_decision")
        g.add_edge("synthesize_decision",    "apply_hard_constraints")
        g.add_edge("apply_hard_constraints", "write_output")
        g.add_edge("write_output",           END)
        return g.compile()

    def _initial_state(self, application_id: str) -> OrchestratorState:
        return OrchestratorState(
            application_id=application_id, session_id=self.session_id,
            credit_result=None, fraud_result=None, compliance_result=None,
            recommendation=None, confidence=None, approved_amount=None,
            executive_summary=None, conditions=None, hard_constraints_applied=[],
            errors=[], output_events=[], next_agent=None,
        )

    async def _node_validate_inputs(self, state):  raise NotImplementedError
    async def _node_load_credit(self, state):      raise NotImplementedError
    async def _node_load_fraud(self, state):       raise NotImplementedError
    async def _node_load_compliance(self, state):  raise NotImplementedError
    async def _node_synthesize(self, state):       raise NotImplementedError
    async def _node_constraints(self, state):      raise NotImplementedError
    async def _node_write_output(self, state):     raise NotImplementedError
