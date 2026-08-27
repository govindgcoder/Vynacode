from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from pydantic import BaseModel, Field, RootModel, computed_field


class Parameter(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None


class Block(BaseModel):
    id: str
    name: Optional[str] = None
    type: Optional[str] = None
    params: Optional[List[Parameter]] = None
    returns: Optional[str] = None
    line_range: Optional[Tuple[int, int]] = None
    dependencies: Optional[List[str]] = None
    summary: Optional[str] = None
    byte_start: int
    byte_end: int
    parent_id: Optional[str] = None
    parent_file: Optional[str] = None
    is_async: bool = False
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
