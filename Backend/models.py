from pydantic import BaseModel, Field

class PathRequest(BaseModel):

    path: str = Field(
        min_length=2,
        max_length=500
    )