# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
from knowledge.business_rules import BUSINESS_RULES


class RuleRetriever:
    """Select the business rules that are injected into a retrieval prompt.

    Every rule in ``BUSINESS_RULES`` is one of two kinds:

    * **Triggered rules** have a key listed in :attr:`RULE_MAPPING`. Such a
      rule is returned only when at least one of its trigger keywords occurs
      in the question (matched case-insensitively as a substring).
    * **Always-on rules** have a key that is absent from :attr:`RULE_MAPPING`,
      typically because a deployment defined the rule in
      ``project_config/business_rules.yaml``. They have no trigger, so they
      apply to every question and are always returned.

    Returned texts are stripped of surrounding whitespace, follow the
    insertion order of ``BUSINESS_RULES`` (the order of the YAML file), and
    appear at most once each. Blank rule texts are skipped, as in the static
    prompt path (:func:`prompt_engine.static_prefix._all_business_rules_text`).

    Attributes
    ----------
    RULE_MAPPING : dict[str, list[str]]
        Rule key to trigger keywords. Keys here make a rule triggered rather
        than always-on.

    Examples
    --------
    >>> from unittest import mock
    >>> rules = {"tax": "Amounts are net of VAT.", "date": "Use DateTable."}
    >>> with mock.patch("retrieval.rule_retriever.BUSINESS_RULES", rules):
    ...     RuleRetriever.retrieve("monthly trade volume")
    ['Amounts are net of VAT.', 'Use DateTable.']
    """

    RULE_MAPPING = {
        "purchase": ["خرید", "purchase"],
        "trade": ["معامله", "معاملات", "trade"],
        "offer": ["عرضه", "offer"],
        "customer": ["مشتری", "خریدار"],
        "supplier": ["عرضه کننده", "فروشنده"],
        "broker": ["کارگزار", "broker"],
        "symbol": ["نماد", "کالا", "symbol"],
        "date": ["تاریخ", "سال", "ماه", "هفته", "فصل", "روز", "year", "month", "week", "season", "day",
                 "شنبه", "جمعه", "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور", "مهر", "آبان",
                 "آذر", "دی", "بهمن", "اسفند", "بهار", "تابستان", "پاییز", "زمستان"],
        "ring": ["تالار", "رینگ", "ring"],
        "topn": ["بیشترین", "برتر", "top"],
    }

    @classmethod
    def retrieve(cls, question: str) -> list[str]:
        """Return the business rules that apply to a question.

        Parameters
        ----------
        question : str
            The natural-language question. Trigger keywords are matched
            case-insensitively as substrings of it.

        Returns
        -------
        list[str]
            Stripped rule texts, in ``BUSINESS_RULES`` order, without
            duplicates and without blank entries. The list contains every
            always-on rule (key not in :attr:`RULE_MAPPING`) and every
            triggered rule whose key has at least one keyword present in
            *question*. A key in :attr:`RULE_MAPPING` with no entry in
            ``BUSINESS_RULES`` contributes nothing.

        Examples
        --------
        >>> from unittest import mock
        >>> rules = {"zeta": "Always apply.", "purchase": "Use FactTable."}
        >>> with mock.patch("retrieval.rule_retriever.BUSINESS_RULES", rules):
        ...     RuleRetriever.retrieve("how many widgets")
        ['Always apply.']
        >>> with mock.patch("retrieval.rule_retriever.BUSINESS_RULES", rules):
        ...     RuleRetriever.retrieve("total purchase value")
        ['Always apply.', 'Use FactTable.']
        """
        q = question.lower()
        triggered = {
            rule_key
            for rule_key, aliases in cls.RULE_MAPPING.items()
            if any(alias.lower() in q for alias in aliases)
        }
        results: list[str] = []
        for rule_key, rule_text in BUSINESS_RULES.items():
            if rule_key in cls.RULE_MAPPING and rule_key not in triggered:
                continue
            text = rule_text.strip()
            if text:
                results.append(text)
        return results
