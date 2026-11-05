from pydantic import BaseModel


class ResearchRequest(BaseModel):
    question: str
