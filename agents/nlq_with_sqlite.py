"""LangChain SQL Agent — Ollama + SQLite (local testing, no SQL Server needed).

Usage (from repo root)::

    python scripts/create_db.py          # create sample.db once
    python -m agents.nlq_with_sqlite --prompt "How many albums?"
"""

from __future__ import annotations

import argparse
import logging
import sys

from sql_agent.config import Settings, load_env_file

from langchain_ollama import ChatOllama
from langchain_community.utilities.sql_database import SQLDatabase
from langchain_community.agent_toolkits import create_sql_agent

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def build_llm(cfg: Settings) -> ChatOllama:
    llm = ChatOllama(
        model=cfg.ollama_model,
        base_url=cfg.ollama_base_url,
        temperature=0.1,
        top_p=0.9,
        streaming=False,
    )
    logger.info("LLM loaded: %s @ %s", cfg.ollama_model, cfg.ollama_base_url)
    return llm


def build_db(cfg: Settings) -> SQLDatabase:
    uri = f"sqlite:///{cfg.sqlite_db_path}"
    db  = SQLDatabase.from_uri(uri)
    logger.info("Connected to SQLite: %s", cfg.sqlite_db_path)
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
    p = argparse.ArgumentParser(description="NLQ over SQLite using Ollama + LangChain.")
    p.add_argument("--prompt", required=True, help="Natural language question")
    return p.parse_args()


def main() -> None:
    load_env_file()
    cfg  = Settings()
    args = parse_args()
    try:
        logger.info("Initializing SQL Agent...")
        llm = build_llm(cfg)
        db  = build_db(cfg)
        logger.info("Testing LLM connection...")
        test = llm.invoke("Reply with OK only.")
        logger.info("LLM warmup: %s", test.content)
        agent  = build_agent(llm, db)
        result = agent.invoke({"input": args.prompt})
        print("\n" + "=" * 60)
        print("QUESTION:", args.prompt)
        print("\nANSWER:")
        print(result["output"])
        print("=" * 60 + "\n")
    except Exception as exc:
        logger.error("Fatal error: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
