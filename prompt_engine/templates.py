#: Static, byte-identical prefix built once per system-prompt version by
#: ``prompt_engine.static_prefix.build_static_prefix`` and reused across every
#: request — see ``docs/api-contract-v2.md`` §8. Section order matters: it is
#: what preserves KV-cache reuse, and must not be changed casually.
STATIC_PREFIX_TEMPLATE = """
{system_prompt}

==================================================
BUSINESS RULES
==================================================

{business_rules}

==================================================
METRICS
==================================================

{metrics}

==================================================
DATABASE SCHEMA
==================================================

{schema}

==================================================
RELATIONSHIPS
==================================================

{relationships}

==================================================
EXAMPLES
==================================================

{examples}
"""

#: Variable suffix appended after :data:`STATIC_PREFIX_TEMPLATE`. Only this
#: part changes between requests: detected value filters, prior-turn session
#: context (empty until sessions land), and the question itself. Never put
#: per-request content anywhere in the prefix above — see the module-level
#: warning in ``prompt_engine.static_prefix``.
SUFFIX_TEMPLATE = """
==================================================
DETECTED FILTERS
==================================================

{filters}

IMPORTANT:

The detected filters are canonical business values.

When generating SQL:

- Use filter values exactly as provided.
- Do not rewrite filter values.
- Do not shorten filter values.
- Do not replace filter values with aliases.
- If Ring = "تالار پتروشیمی" then SQL must use:

r.Name = N'تالار پتروشیمی'

NOT:

r.Name = N'پتروشیمی'

==================================================
SESSION CONTEXT
==================================================

{session_context}

==================================================
USER QUESTION
==================================================

{question}

SQL:
"""

#: Legacy per-question template — kept for the retrieval fallback path (used
#: when ``prompt_engine.static_prefix.should_use_static_prefix`` is False,
#: i.e. a knowledge base too large for ``STATIC_PREFIX_TEMPLATE`` to be
#: economical). See ``prompt_engine.builder.PromptBuilder._build_retrieval``.
PROMPT_TEMPLATE = """
{system_prompt}

==================================================
BUSINESS RULES
==================================================

{business_rules}

==================================================
DATABASE SCHEMA
==================================================

{schema}

==================================================
RELATIONSHIPS
==================================================

{relationships}

==================================================
DETECTED FILTERS
==================================================

{filters}

IMPORTANT:

The detected filters are canonical business values.

When generating SQL:

- Use filter values exactly as provided.
- Do not rewrite filter values.
- Do not shorten filter values.
- Do not replace filter values with aliases.
- If Ring = "تالار پتروشیمی" then SQL must use:

r.Name = N'تالار پتروشیمی'

NOT:

r.Name = N'پتروشیمی'

==================================================
EXAMPLES
==================================================

{examples}

==================================================
USER QUESTION
==================================================

{question}

SQL:
"""
