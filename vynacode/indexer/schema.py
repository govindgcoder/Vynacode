from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from pydantic import BaseModel, Field, RootModel


class Parameter(BaseModel):
    name: str
    type: str


class Block(BaseModel):
    id: str
    name: str
    type: str
    params: List[Parameter]
    returns: str
    line_range: Tuple[int, int]
    dependencies: List[str]
    summary: str


class FileMetadata(BaseModel):
    name: str
    path: Path
    language: str
    size_bytes: int = Field(..., ge=0)
    hash: str
    imports: List[str] = Field(default_factory=list)
    blocks: List[Block] = Field(default_factory=list)


# to prevent any negative value ge is used
class SystemStats(BaseModel):
    files: int = Field(..., ge=0)
    blocks: int = Field(..., ge=0)
    tokens_used: int = Field(..., ge=0)


class VynacodeManifest(BaseModel):
    vynacode_version: str
    indexed_at: datetime
    root: Path
    stats: SystemStats
    files: List[FileMetadata]
