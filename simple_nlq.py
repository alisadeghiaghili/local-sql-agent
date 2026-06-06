# -*- coding: utf-8 -*-
"""
Local SQL Agent — Gemma 3 (Ollama) + SQL Server + LangChain

Answers natural language questions about a SQL Server database
using a fully local LLM. No data leaves the machine.

Usage:
    python simple_nlq.py

Requirements:
    pip install -r requirements.txt
    ollama pull gemma3:12b
"""

def warn(*args, **kwargs):
    pass

import warnings
warnings.warn = warn
warnings.filterwarnings("ignore")

import sys
import logging
from config import get_ollama_config, get_sqlserver_uri

from langchain_ollama import ChatOllama
from langchain.agents import AgentType
from langchain_community.utilities.sql_database import SQLDatabase
from langchain_community.agent_toolkits import create_sql_agent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def build_llm() -> ChatOllama:
    """Build ChatOllama instance from environment config."""
    cfg = get_ollama_config()
    llm = ChatOllama(
        model=cfg["model"],
        base_url=cfg["base_url"],
        temperature=cfg["temperature"],
        top_p=cfg["top_p"],
    )
    logger.info(f"LLM loaded: {cfg['model']} @ {cfg['base_url']}")
    return llm


def build_db() -> SQLDatabase:
    """Build SQLDatabase instance from environment config."""
    uri = get_sqlserver_uri()
    db  = SQLDatabase.from_uri(uri)
    logger.info("SQL Server connection established")
    return db


def build_agent(llm: ChatOllama, db: SQLDatabase):
    """Build LangChain SQL agent executor."""
    agent = create_sql_agent(
        llm=llm,
        db=db,
        verbose=True,
        handle_parsing_errors=True,
        agent_type=AgentType.ZERO_SHOT_REACT_DESCRIPTION,
    )
    logger.info("SQL agent created")
    return agent


def main():
    """Main entry point."""
    try:
        logger.info("Initializing Local SQL Agent (Gemma 3 + SQL Server)...")

        llm   = build_llm()
        db    = build_db()
        agent = build_agent(llm, db)

        question = "How many records are in the database?"
        logger.info(f"Query: {question}")

        result = agent.invoke(question)
        print("\n" + "=" * 60)
        print("Answer:", result)
        print("=" * 60 + "\n")

    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        logger.error("Steps to fix:")
        logger.error("  1. Install Ollama — https://ollama.com")
        logger.error("  2. Run: ollama pull gemma3:12b")
        logger.error("  3. Copy .env.example to .env and fill in your values")
        logger.error("  4. Run: python simple_nlq.py")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n⚠️  Cancelled by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
