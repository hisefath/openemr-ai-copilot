"""Deterministic citation check: a claim survives only if its source was returned by a tool
in THIS request and the quoted value actually appears in that record."""
import json
from typing import Dict, List

from pydantic import BaseModel, Field

NOT_FOUND = "not found in record"


class Claim(BaseModel):
    text: str = Field(description="One clinical fact, in plain English")
    source_id: str = Field(description="ResourceType/id of the record this fact comes from, e.g. MedicationRequest/42")
    quoted_value: str = Field(description="The exact value copied from that record that supports the fact")


class Briefing(BaseModel):
    claims: List[Claim]
    answer: str = Field(description="Short answer to the clinician's question, using only the claims above")


class VerifiedClaim(Claim):
    verified: bool


def verify(briefing: Briefing, fetched: Dict[str, dict]) -> List[VerifiedClaim]:
    """fetched maps source_id -> the record dict the model was shown."""
    out = []
    for c in briefing.claims:
        record = fetched.get(c.source_id)
        ok = (
            record is not None
            and bool(c.quoted_value.strip())
            and c.quoted_value.strip().lower() in json.dumps(record).lower()
        )
        data = c.model_dump()
        if not ok:
            data["text"] = f"{c.text} ({NOT_FOUND})"
        out.append(VerifiedClaim(**data, verified=ok))
    return out


if __name__ == "__main__":
    fetched = {"AllergyIntolerance/7": {"source_id": "AllergyIntolerance/7", "substance": "Penicillin", "reactions": ["Hives"]}}
    b = Briefing(
        answer="x",
        claims=[
            Claim(text="Allergic to penicillin", source_id="AllergyIntolerance/7", quoted_value="penicillin"),
            Claim(text="Allergic to latex", source_id="AllergyIntolerance/7", quoted_value="latex"),
            Claim(text="On warfarin", source_id="MedicationRequest/99", quoted_value="warfarin"),
            Claim(text="Empty quote", source_id="AllergyIntolerance/7", quoted_value="  "),
        ],
    )
    assert [v.verified for v in verify(b, fetched)] == [True, False, False, False]
    assert NOT_FOUND in verify(b, fetched)[2].text
    print("verify ok")
