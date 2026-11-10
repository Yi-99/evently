"""REST routes for the intake form (mounted under /api/v1)."""

from typing import Annotated

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, create_model

from app.core.deps import Ctx, CurrentUser

router = APIRouter(tags=["intake"])

SYSTEM = """You fill in an event-planning intake form from what the user wrote.
For each field, copy what the user actually said, in a short phrase. Use null for anything they did not state.
Never guess or invent values. "headcount" is a whole number of guests. "city" is just the city or venue name."""


class IntakeQuestion(BaseModel):
    id: Annotated[str, StringConstraints(pattern=r"^[a-z]+$", max_length=40)]
    prompt: Annotated[str, StringConstraints(max_length=500)]


class IntakeExtractIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    text: Annotated[str, StringConstraints(max_length=20_000)]
    event_type: Annotated[str, StringConstraints(max_length=200)] = Field(alias="eventType")
    questions: Annotated[list[IntakeQuestion], Field(max_length=40)]


class IntakeExtractOut(BaseModel):
    answers: dict[str, str | int]


@router.post("/intake/extract", response_model=IntakeExtractOut)
async def extract_intake(body: IntakeExtractIn, ctx: Ctx, user: CurrentUser):
    """Read a free-text event description and return only the intake answers the user actually stated."""
    if not ctx.llm.available:
        raise HTTPException(503, "Claude is not configured. Set ANTHROPIC_API_KEY in .env.")
    if not body.questions:
        return {"answers": {}}

    fields = {
        q.id: (int | None if q.id == "headcount" else str | None, Field(default=None, description=q.prompt))
        for q in body.questions
    }
    output_model = create_model("IntakeAnswers", **fields)  # type: ignore[call-overload]
    result = await ctx.llm.parse(
        purpose="intake_extract",
        system=SYSTEM,
        messages=[{"role": "user", "content": f"Event type: {body.event_type}\n\nUser wrote:\n{body.text}"}],
        output_model=output_model,
        tier="worker",
        effort="low",
        max_tokens=2048,
    )
    # Drop nulls and empty strings so the client only sees what the user stated.
    answers = {k: v for k, v in result.model_dump().items() if v is not None and v != ""}
    return {"answers": answers}
