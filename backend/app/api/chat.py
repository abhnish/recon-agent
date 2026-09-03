"""
chat.py
───────
POST /api/chat — natural-language Q&A over reconciliation results.

The Q&A layer uses simple heuristic context retrieval (not vector RAG)
against the current exception list, then calls Gemini to generate an answer
grounded in that context.

⚠️  LLM MATCHING PROHIBITION: The chat endpoint passes already-classified
    results to the LLM layer for Q&A only.  The LLM does not influence any
    matching or classification decision.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.schemas import ChatRequest, ChatResponse
from app.api.state import app_state
from app.services.llm_layer import answer_question

router = APIRouter(prefix="/api", tags=["chat"])


@router.post(
    "/chat",
    response_model=ChatResponse,
    summary="Natural-language Q&A about reconciliation results",
    description=(
        "Accepts a natural-language question about the current reconciliation "
        "results and returns an answer grounded in the retrieved context.  "
        "Context retrieval is heuristic (order ID → subtype → summary → status); "
        "no vector embedding or RAG is used.  "
        "If the question cannot be answered from the data, the response says so "
        "explicitly rather than guessing.  "
        "Returns 409 if no reconciliation run has been executed."
    ),
)
def chat(request: ChatRequest) -> ChatResponse:
    """Answer a natural-language question about the current results.

    Args:
        request: A ChatRequest containing the user's question.

    Returns:
        A ChatResponse with the answer, context summary, and llm_status.

    Raises:
        HTTPException 409: If no reconcile run has been executed.
    """
    if not app_state.is_ready():
        raise HTTPException(
            status_code=409,
            detail="No reconciliation run found. Call POST /api/reconcile/run first.",
        )

    # Build context from exception diffs (serialised to dicts)
    context = [d.to_dict() for d in app_state.exception_diffs.values()]

    qa_resp = answer_question(request.question, context)

    # Log audit entry
    app_state.add_audit_entry(qa_resp.audit_entry)

    return ChatResponse(
        question=qa_resp.question,
        answer=qa_resp.answer,
        context_used=qa_resp.context_used,
        llm_status=qa_resp.llm_status,
    )
