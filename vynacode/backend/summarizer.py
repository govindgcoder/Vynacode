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
                "<task>Write one search-index summary for each code block below.</task>\n"
                "<rules>\n"
                "- Under 40 words. Say what it does and returns, not how.\n"
                "- Repeat the exact identifiers it defines and calls, verbatim.\n"
                "</rules>\n"
                "<output>\n"
                'Return ONLY {"summaries": {"<block id>": "<summary>"}} using the given ids.\n'
                'Example: {"summaries": {"abc123": "Parses a file into Block objects."}}\n'
                "</output>\n\n"
                f"<blocks>\n{code_xml}</blocks>"
            )
            parsed = None
            retry_prompt = prompt
            for attempt in range(3):
                output = await llm.complete(
                    # Non-thinking: extraction, not reasoning -- a long think
                    # trace would eat the num_predict budget and truncate the JSON.
                    PLANNER_MODEL, "user", retry_prompt, think=False,
                    format=BlockSummaries.model_json_schema(),
                )
                try:
                    parsed = BlockSummaries.model_validate_json(output)
                    break
                except Exception as e:
                    print(f"Error parsing JSON (attempt {attempt + 1}/3): {e}")
                    print("Raw output:", output)
                    retry_prompt = (
                        prompt
                        + f"\n\nPREVIOUS ERROR: {e}\nFix the JSON structure and try again."
                    )
            batch = current_batch
            current_batch = []
            current_tokens = 0
            if parsed is None:
                # This batch only. The next one is independent, and the run moves
                # on to the following file.
                print("Failed to parse summaries after 3 attempts; skipping this batch.")
                print(f"processed {no_of_blocks}/{total_no} in {batch[0].parent_file}")
                continue

            update_data = []
            for block in batch:
                summary = parsed.summaries.get(block.id)
                if summary:
                    update_data.append((summary, block.id))

            if update_data:
                with sqlite3.connect(db_path) as conn:
                    conn.executemany("UPDATE blocks SET summary = ? WHERE id = ?", update_data)

            print(f"processed {no_of_blocks}/{total_no} in {batch[0].parent_file}")
