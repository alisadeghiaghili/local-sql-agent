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