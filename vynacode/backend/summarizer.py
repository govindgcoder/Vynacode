import sqlite3
from pathlib import Path
from typing import Dict, List

from pydantic import BaseModel

from schema import Block
from client import OllamaClient
from config import PLANNER_MODEL, TOKEN_BUDGET


class BlockSummaries(BaseModel):
    summaries: Dict[str, str]


async def summarize_and_store(llm: OllamaClient, db_path: Path, blocks: List[Block], source_code: bytes):
    current_batch: List[Block] = []
    current_tokens = 0
    update_data = []

    no_of_blocks = 0
    total_no = len(blocks)
    for blk in blocks:
        current_batch.append(blk)
        current_tokens += blk.token_estimate
        no_of_blocks += 1
        if current_tokens >= TOKEN_BUDGET or no_of_blocks == total_no:
            code_xml = ""
            for block in current_batch:
                code_bytes = source_code[block.byte_start:block.byte_end]
                code_string = code_bytes.decode("utf-8", errors="replace")
                code_xml += f'<block id="{block.id}">\n{code_string}\n</block>\n'

            prompt = (
                "Summarize the functionality of each code block in less than 40 words. "
                "Return a JSON object with a single key \"summaries\" whose value is an object "
                "mapping each block id to its summary. Use the exact block ids provided. "
                "Output ONLY the JSON object, no other text.\n\n"
                f"CODE BLOCKS:\n{code_xml}"
            )
            output = await llm.complete(
                PLANNER_MODEL, "user", prompt, format=BlockSummaries.model_json_schema()
            )
            try:
                parsed = BlockSummaries.model_validate_json(output)
            except Exception as e:
                print(f"Error parsing JSON: {e}")
                print("Raw output:", output)
                return

            for block in current_batch:
                summary = parsed.summaries.get(block.id)
                if summary:
                    update_data.append((summary, block.id))

            current_batch = []
            current_tokens = 0

    if update_data:
        with sqlite3.connect(db_path) as conn:
            conn.executemany("UPDATE blocks SET summary = ? WHERE id = ?", update_data)
