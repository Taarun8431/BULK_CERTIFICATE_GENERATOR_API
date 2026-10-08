#!/usr/bin/env python3
"""
large_request_generator.py – Generate a large sample request JSON body.

Usage:
    python sample_data/large_request_generator.py          # 500 recipients
    python sample_data/large_request_generator.py 1000     # 1000 recipients
    python sample_data/large_request_generator.py > big.json
"""

import json
import sys


def generate(n: int = 500) -> dict:
    return {
        "event_name": f"Large Scale Event {n} Participants",
        "organization": "Bulk Org",
        "description": "for successfully completing the course",
        "issue_date": "2026-10-08",
        "signatory_name": "A. Manager",
        "signatory_title": "Event Director",
        "recipients": [
            {
                "name": f"Participant {i:04d}",
                "email": f"participant{i:04d}@example.com",
            }
            for i in range(1, n + 1)
        ],
    }


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    print(json.dumps(generate(n), indent=2))
