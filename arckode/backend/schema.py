from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from pydantic import BaseModel, Field, computed_field, field_validator


class ExpandQueryResponse(BaseModel):
    keywords: str


class PlanStep(BaseModel):
    file: str
    instruction: str


class PlanResponse(BaseModel):
    steps: List[PlanStep]


class RunAction(BaseModel):
    command: str


class EditAction(BaseModel):
    """Anchor-based edit. Line numbers are resolved by the editor, not the model."""
    file_path: str
    anchor: str
    line_count: int = Field(1, ge=1)
    end_anchor: Optional[str] = None
    occurrence: int = Field(1, ge=1)
    new_text: str

    @field_validator("anchor", "end_anchor", mode="before")
    @classmethod
    def _text_anchor(cls, v):
        return str(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v


class FullWriteAction(BaseModel):
    """Whole-file overwrite. The editor returns the resulting unified diff."""
    file_path: str
    content: str


class FreeResponse(BaseModel):
    # Optional: only reasoning models produce it, so it is never requested.
    thought: str = ""
    response: str = ""
    edit: Optional[EditAction] = None
    write: Optional[FullWriteAction] = None
    run: Optional[List[RunAction]] = None


class Block(BaseModel):
    id: str
    name: Optional[str] = None
    type: Optional[str] = None
    signature: Optional[str] = None
    line_range: Optional[Tuple[int, int]] = None
    dependencies: Optional[List[str]] = None
    summary: Optional[str] = None
    byte_start: int
    byte_end: int
    parent_id: Optional[str] = None
    parent_file: Optional[str] = None
    chunk_boundary: bool = False

    @computed_field
    @property
    def token_estimate(self) -> int:
        return (self.byte_end - self.byte_start) // 3


class FileMetaData(BaseModel):
    name: str
    path: Path
    language: str
    size_bytes: int = Field(..., ge=0)
    hash: str
    imports: List[str] = Field(default_factory=list)
    blocks: List[Block] = Field(default_factory=list)
