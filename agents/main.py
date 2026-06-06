# -*- coding: utf-8 -*-
"""LangChain SQL Agent — Gemma 3 (Ollama) + SQL Server.

Usage (from repo root)::

    python -m agents.main --prompt "How many albums are in the database?"
"""

from __future__ import annotations

import argparse
import logging
import sys

from sql_agent.config import Settings, load_env_file

from langchain_ollama import ChatOllama
from langchain.agents import AgentType
from langchain_community.utilities.sql_database import SQLDatabase
from langchain_community.agent_toolkits import create_sql_agent

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def build_llm(cfg: Settings) -> ChatOllama:
    llm = ChatOllama(
        model=cfg.ollama_model,
        base_url=cfg.ollama_base_url,
        temperature=cfg.ollama_temperature,
        top_p=cfg.ollama_top_p,
    )
    logger.info("LLM loaded: %s @ %s", cfg.ollama_model, cfg.ollama_base_url)
    return llm


def build_db(cfg: Settings) -> SQLDatabase:
    db = SQLDatabase.from_uri(cfg.sqlserver_uri())
    logger.info("SQL Server connection established")
    return db


def build_agent(llm: ChatOllama, db: SQLDatabase):
    agent = create_sql_agent(
        llm=llm,
        db=db,
        verbose=True,
        handle_parsing_errors=True,
        agent_type=AgentType.ZERO_SHOT_REACT_DESCRIPTION,
    )
    logger.info("SQL agent created")
    return agent


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ask natural language questions about your SQL Server database.")
    p.add_argument("--prompt", type=str, help="The question to send to the SQL agent")
    return p.parse_args()


def main() -> None:
    load_env_file()
    cfg  = Settings()
    cfg.validate()
    args = parse_args()

    if not args.prompt:
        print('Usage: python -m agents.main --prompt "your question"')
        sys.exit(1)

    try:
        logger.info("Initializing Local SQL Agent...")
        llm   = build_llm(cfg)
        db    = build_db(cfg)
        agent = build_agent(llm, db)

        logger.info("Query: %s", args.prompt)
        result = agent.invoke(args.prompt)

        print("\n" + "=" * 60)
        print("Answer:", result)
        print("=" * 60 + "\n")

    except KeyboardInterrupt:
        print("\n⚠️  Cancelled by user")
        sys.exit(1)
    except Exception as exc:
        logger.error("Fatal error: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
