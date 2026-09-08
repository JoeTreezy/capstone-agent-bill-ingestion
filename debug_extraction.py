"""Diagnostic: calls Claude directly with one PDF and prints everything about
the raw response, so we can see exactly what came back instead of guessing
why json.loads() failed on it.

Usage: python debug_extraction.py /path/to/8838484.pdf
"""
import base64
import sys

import anthropic
from config import settings
from extraction import EXTRACTION_SYSTEM_PROMPT

if len(sys.argv) != 2:
    print("Usage: python debug_extraction.py /path/to/bill.pdf")
    sys.exit(1)

pdf_bytes = open(sys.argv[1], "rb").read()
pdf_b64 = base64.b64encode(pdf_bytes).decode("utf-8")

client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

response = client.messages.create(
    model=settings.anthropic_model,
    max_tokens=4096,
    system=EXTRACTION_SYSTEM_PROMPT,
    messages=[{
        "role": "user",
        "content": [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64}},
            {"type": "text", "text": "Extract this bill."},
        ],
    }],
)

print("=== stop_reason ===")
print(response.stop_reason)
print()
print("=== usage ===")
print(response.usage)
print()
print("=== content blocks (types present) ===")
for i, block in enumerate(response.content):
    print(f"  [{i}] type={block.type}")
print()
print("=== full text of every text-type block ===")
for i, block in enumerate(response.content):
    if block.type == "text":
        print(f"--- block [{i}] ---")
        print(repr(block.text))  # repr() so empty strings and whitespace are visible, not just invisible
print()
print("=== any non-text block content (in case that's where the actual output is) ===")
for i, block in enumerate(response.content):
    if block.type != "text":
        print(f"--- block [{i}] ({block.type}) ---")
        print(block)