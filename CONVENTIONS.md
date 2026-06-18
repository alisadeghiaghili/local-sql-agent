\# Project Context



\## What this project does

Local SQL agent that answers questions about database using LLM.



\## Architecture decisions made

\- Skills system: reusable prompt modules in skills/ package

\- Refiner layer: post-processing pipeline in refiner/ package

\- LiteLLM proxy on localhost:4000 for model routing



\## Coding conventions

\- Use async/await throughout

\- All LLM calls go through LLMBackend abstraction

\- No magic strings — use enums

\- No exceptions for control flow



\## Current phase

Phase 1: Building skills/ package

