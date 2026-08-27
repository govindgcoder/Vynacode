import sqlite3
import re
import json
from pathlib import Path
from typing import List

from schema import Block 
from client import OllamaClient

import sys
sys.path.append('../')
from config import PLANNER_MODEL, TOKEN_BUDGET

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
                "Summarize the functionality of the following code blocks, in less than 40 words each. "
                "Output your summaries in JSON format: {\"id1\": \"summary1\", \"id2\": \"summary2\"}. "
                "Ensure output is ONLY the JSON object. No other text.\n\n"
                f"CODE BLOCKS:\n{code_xml}"
            )
            output = await llm.complete(PLANNER_MODEL, "user", prompt)
            output = output.strip()
            output = re.sub(r"^```(?:json)?\s*", "", output)
            output = re.sub(r"\s*```$", "", output)
            try:
                output_json = json.loads(output)
            except Exception as e:
                print(f"Error parsing JSON: {e}")
                return

            for block in current_batch:
                summary = output_json.get(block.id)
                if summary:
                    update_data.append((summary, block.id))

            current_batch = []
            current_tokens = 0

    if update_data:
        with sqlite3.connect(db_path) as conn:  
            conn.executemany("UPDATE blocks SET summary = ? WHERE id = ?", update_data)

