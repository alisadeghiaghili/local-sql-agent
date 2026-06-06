# -*- coding: utf-8 -*-
"""
Local SQL Agent — Remote Ollama + SQLite + LangChain

Usage:
    python nlq_with_sqlite.py --prompt "How many albums are in the database?"
"""

import warnings
warnings.filterwarnings("ignore")

import sys
import argparse
import logging
import os

from langchain_ollama import ChatOllama
from langchain_community.utilities.sql_database import SQLDatabase
from langchain_community.agent_toolkits import create_sql_agent


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger(__name__)


OLLAMA_BASE_URL = os.getenv(
    "OLLAMA_BASE_URL",
    "http://ai.ime.co.ir/ollama"
)

OLLAMA_MODEL = os.getenv(
    "OLLAMA_MODEL",
    "gemma3:27b"
)

SQLITE_DB_PATH = os.getenv(
    "SQLITE_DB_PATH",
    "sample.db"
)


def build_llm() -> ChatOllama:

    llm = ChatOllama(
        model=OLLAMA_MODEL,
        base_url=OLLAMA_BASE_URL,
        temperature=0.1,
        top_p=0.9,
        streaming=False,
    )

    logger.info(
        f"LLM loaded: {OLLAMA_MODEL} @ {OLLAMA_BASE_URL}"
    )

    return llm


def build_db() -> SQLDatabase:

    uri = f"sqlite:///{SQLITE_DB_PATH}"

    db = SQLDatabase.from_uri(uri)

    logger.info(
        f"Connected to SQLite database: {SQLITE_DB_PATH}"
    )

    return db


def build_agent(llm: ChatOllama, db: SQLDatabase):

    agent = create_sql_agent(
        llm=llm,
        db=db,
        agent_type="zero-shot-react-description",
        verbose=True,
        handle_parsing_errors=True,
    )

    logger.info("SQL agent created")

    return agent


def parse_args() -> argparse.Namespace:

    parser = argparse.ArgumentParser(
        description="Ask natural language questions about your SQLite database using Ollama + LangChain."
    )

    parser.add_argument(
        "--prompt",
        type=str,
        required=True,
        help="Natural language question"
    )

    return parser.parse_args()


def main():

    args = parse_args()

    try:

        logger.info(
            "Initializing SQL Agent..."
        )

        llm = build_llm()
        db = build_db()

        logger.info("Testing LLM connection...")

        test_response = llm.invoke("Reply with OK only.")

        logger.info(
            f"LLM test response: {test_response.content}"
        )

        agent = build_agent(llm, db)

        logger.info(f"User Query: {args.prompt}")

        result = agent.invoke({
            "input": args.prompt
        })

        print("\n" + "=" * 60)

        print("QUESTION:")
        print(args.prompt)

        print("\nANSWER:")
        print(result["output"])

        print("=" * 60 + "\n")

    except Exception as e:

        logger.error(
            f"Fatal error: {e}",
            exc_info=True
        )

        sys.exit(1)


if __name__ == "__main__":
    main()
