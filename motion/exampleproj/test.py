import motion
import os

from schemas import QuerySource
from mconfig import MCONFIG
from rich import print

# Test that for simple queries, the results make some sense


def test_ask_chatbot():
    connection = motion.test(
        MCONFIG,
        wait_for_triggers=[],  # No triggers to wait for
        motion_logging_level="INFO",
    )

    all_prompts = [
        "What should I wear to a wedding?",
        "What should I wear to a party?",
        "What should I wear to a job interview?",
        "What should I wear to a first date?",
        "What should I wear to a picnic?",
    ]

    for prompt in all_prompts:
        new_id = connection.getNewId(namespace="chat")
        # Must specify kw for every arg in .set and .get
        connection.set(
            namespace="chat",
            identifier=new_id,
            key_values={"prompt": prompt},
        )
        result = connection.get(
            namespace="chat", identifier=new_id, keys=["completion"]
        )
        print(f"Prompt: {prompt} and result: {result}")

    connection.close(wait=False)


test_ask_chatbot()
